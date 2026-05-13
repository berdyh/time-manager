"""Single-writer daemon over a Unix domain socket.

Architecture
------------
- One :class:`TMDaemon` process owns ALL SQLite writes.  Clients (CLI, future
  Telegram bot) connect to the socket and send line-delimited JSON requests.
- Reads bypass the socket entirely — clients open SQLite in WAL read mode for
  concurrent reads, which is safe alongside the daemon's writer.
- One :class:`~tm.llm.cost_meter.CostMeter` instance per daemon: gates LLM
  calls so the soft-alarm fires at most once per process (the meter's
  ``_soft_alarm_fired`` flag is per-instance — a long-lived singleton inside
  the daemon is exactly the right shape).
- Default socket path: ``~/.local/share/tm/tm.sock``; mode ``0600``
  (owner-only, so the Unix permission system *is* the auth boundary in v1).
- ``SIGTERM`` / ``SIGINT`` → graceful shutdown when ``run()`` is the main
  thread; otherwise callers invoke :meth:`TMDaemon.shutdown` explicitly.

Threading model
---------------
- ``run()`` runs the accept loop on the calling thread; each accepted
  connection is handled by a dedicated worker thread (one-thread-per-conn —
  v1 traffic is low and this keeps the implementation small and obvious).
- A single coarse :class:`threading.Lock` (``self._lock``) serialises most
  writes.  Repository write methods open per-call ``sqlite3.connect``s; this
  lock prevents two daemon-side writers from racing.  Note: SQLite WAL also
  protects against external readers, but two simultaneous writers can still
  see ``database is locked``; this lock side-steps that entirely.
- Read paths (``CostMeter.monthly_total``, repos' ``get`` / ``list``) are NOT
  taken under the lock — WAL handles read concurrency natively and the lock
  exists only to serialise *writes*.
- LLM-backed write handlers (see ``_LLM_METHODS``) ALSO bypass ``self._lock``
  so a long-running LLM call doesn't block sibling RPC writes (e.g. a user
  typing ``tm goal add`` while cron is debriefing). SQLite WAL plus the
  per-call ``sqlite3.connect`` busy_timeout in the repositories handles the
  rare case where two LLM handlers commit simultaneously.
- Per the carry-forward intel from T-FND-02: ``SQLiteStore`` itself uses one
  per-Store connection and busy-retries against external contention, but does
  not protect against two ``Store`` instances in the same process writing
  concurrently. The daemon avoids constructing additional Stores; it does the
  one-time migration apply at startup and then delegates writes to
  per-call-connect repositories under ``self._lock``.

Wire protocol
-------------
Line-delimited JSON, one object per line, both directions.  Encoding is
UTF-8.  Maximum request size is :data:`MAX_REQUEST_BYTES` per line.

Request::

    {"method": "<name>", "params": { ... }}

Response::

    {"ok": true, "result": <any>}     -- success
    {"ok": false, "error": "<short>"} -- failure

Errors are caught at the dispatch boundary; a malformed request never crashes
the daemon — it sends back ``{"ok": false, "error": "..."}`` and the
connection continues until idle-timeout or EOF.

Out of scope (deliberately deferred)
------------------------------------
- No CLI command (``tm daemon start/stop``) — separate follow-up.
- No Telegram bot integration.
- No bot whitelist / auth — the ``0600`` socket is the auth boundary.
- No SQLCipher / keyring beyond the stub in :mod:`tm.resilience`.
- No inflight-call WAL recovery on crash — future resilience task.
- No async I/O (asyncio / anyio / Trio).  Stdlib socket + threading only.
"""

from __future__ import annotations

import errno
import functools
import json
import logging
import os
import select
import socket
import stat
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from tm._paths import default_data_dir
from tm.engines.process_mining import ProcessMiner
from tm.engines.variant_cluster import VariantClusterer
from tm.llm.anthropic_adapter import ANTHROPIC_API_KEY_ENV, AnthropicAdapter
from tm.llm.client import LLMClient
from tm.llm.cost_meter import CostMeter
from tm.llm.errors import CostCapExceeded, LLMClientError
from tm.models.outcome import OutcomeAggregator
from tm.repositories.events import EventsRepository
from tm.repositories.goals import GoalsRepository
from tm.repositories.telemetry import SuggestionTelemetryRepository
from tm.repositories.vocabulary import VocabularyRepository
from tm.stores.kuzu_projection import rebuild_kuzu_projection
from tm.stores.kuzu_store import KuzuStore
from tm.stores.sqlite_store import SQLiteStore
from tm.vocab_alignment import VocabAligner

__all__ = [
    "DEFAULT_SOCKET_NAME",
    "DaemonClient",
    "DaemonRequest",
    "DaemonResponse",
    "MAX_REQUEST_BYTES",
    "SOCKET_BACKLOG",
    "SOCKET_TIMEOUT_S",
    "TMDaemon",
]


DEFAULT_SOCKET_NAME = "tm.sock"
"""Filename for the daemon's Unix domain socket inside the data dir."""

SOCKET_BACKLOG = 16
"""``listen()`` backlog — small on purpose; v1 has at most a handful of clients."""

SOCKET_TIMEOUT_S = 30.0
"""Per-connection idle timeout in seconds.  Bounds zombie connections."""

MAX_REQUEST_BYTES = 1 << 20  # 1 MiB
"""Maximum number of bytes a single request line may contain.

Protects against memory exhaustion if a client (or a buggy peer) streams data
without a newline.  1 MiB is plenty for daemon RPC payloads.
"""

ACCEPT_LOOP_TIMEOUT_S = 0.25
"""``select`` timeout in the accept loop.  Bounds shutdown latency."""

WORKER_JOIN_TIMEOUT_S = 5.0
"""Maximum time :meth:`TMDaemon.shutdown` waits for in-flight worker threads."""


_log = logging.getLogger(__name__)


# Dispatch classification for RPC methods.
#
# - ``_READ_METHODS``: read-only handlers; no daemon lock taken.
# - ``_LLM_METHODS``: LLM-backed write handlers that bypass the daemon's
#   coarse write lock so a long-running LLM call doesn't block sibling RPC
#   writes. SQLite WAL + per-call ``sqlite3.connect`` busy_timeout handle the
#   rare case where two LLM handlers commit simultaneously.
# - Everything else: classic write handler, serialised under ``self._lock``.
_READ_METHODS = frozenset({"ping", "check_budget"})
_LLM_METHODS = frozenset({"run_debrief", "propose_suggestion"})


def _build_llm_client(model: str, max_tokens: int) -> LLMClient:
    """Construct the LLMClient used by the daemon's LLM-backed handlers.

    Tests patch this factory (rather than ``AnthropicAdapter`` directly) to
    inject a mock LLMClient without spending real API tokens. The factory is
    a module-level function so ``unittest.mock.patch`` resolves it cleanly.
    """
    return AnthropicAdapter(model=model, max_tokens=max_tokens)


def _llm_envelope(
    fn: Callable[[TMDaemon, dict[str, Any]], dict[str, Any]],
) -> Callable[[TMDaemon, dict[str, Any]], dict[str, Any]]:
    """Wrap an LLM-backed RPC handler with the standard JSON error envelope.

    Returns ``{"ok": False, "error": "MissingApiKey", ...}`` if the API key
    env var is unset, otherwise runs the wrapped handler and converts any
    raised exception to a structured error envelope. The success path passes
    through unchanged (the wrapped handler returns its own success dict).
    """

    @functools.wraps(fn)
    def wrapped(self: TMDaemon, params: dict[str, Any]) -> dict[str, Any]:
        if not os.environ.get(ANTHROPIC_API_KEY_ENV):
            return {
                "ok": False,
                "error": "MissingApiKey",
                "detail": f"{ANTHROPIC_API_KEY_ENV} environment variable is not set",
            }
        try:
            return fn(self, params)
        except LLMClientError as exc:
            return {"ok": False, "error": "LLMClientError", "detail": str(exc)}
        except Exception as exc:  # noqa: BLE001 — RPC contract returns structured errors
            return {"ok": False, "error": type(exc).__name__, "detail": str(exc)}

    return wrapped


# ---------------------------------------------------------------------- types


@dataclass(frozen=True)
class DaemonRequest:
    """Parsed inbound request: ``{"method": str, "params": dict}``."""

    method: str
    params: dict[str, Any]

    @classmethod
    def from_json(cls, raw: bytes | str) -> DaemonRequest:
        """Parse one JSON line into a :class:`DaemonRequest`.

        Raises :class:`ValueError` for malformed input — the daemon dispatch
        loop catches this and returns a structured error response.
        """
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        obj = json.loads(raw)
        if not isinstance(obj, dict):
            raise ValueError("request must be a JSON object")
        method = obj.get("method")
        if not isinstance(method, str) or not method:
            raise ValueError("request 'method' must be a non-empty string")
        params = obj.get("params", {})
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise ValueError("request 'params' must be an object")
        return cls(method=method, params=params)


@dataclass(frozen=True)
class DaemonResponse:
    """Outbound response.

    On success: ``{"ok": True, "result": <any-json-serialisable>}``.
    On error: ``{"ok": False, "error": "<short message>"}``.
    """

    ok: bool
    result: Any
    error: str | None

    @classmethod
    def success(cls, result: Any) -> DaemonResponse:
        return cls(ok=True, result=result, error=None)

    @classmethod
    def failure(cls, message: str) -> DaemonResponse:
        return cls(ok=False, result=None, error=message)

    def to_json(self) -> bytes:
        """Serialise as one UTF-8 JSON line terminated with ``\\n``."""
        if self.ok:
            payload: dict[str, Any] = {"ok": True, "result": self.result}
        else:
            payload = {"ok": False, "error": self.error}
        return (json.dumps(payload, default=str) + "\n").encode("utf-8")


# --------------------------------------------------------------------- daemon


class TMDaemon:
    """Single-writer daemon. Construct then call :meth:`run` to listen.

    Construction is cheap: we apply pending migrations and build the cost
    meter, but do NOT bind the socket until :meth:`run` is invoked.  This
    matches the testing pattern (start a daemon thread, call ``run`` inside
    it, signal shutdown from the test thread).

    Parameters
    ----------
    db_path:
        Path to the SQLite database.  Defaults to
        :func:`tm._paths.default_data_dir` ``/ "tm.db"``.
    socket_path:
        Path to the Unix socket.  Defaults to
        :func:`tm._paths.default_data_dir` ``/ "tm.sock"``.
    cost_meter:
        Optional pre-built :class:`CostMeter`.  If omitted, the daemon builds
        one against ``db_path``.  In production a single instance lives for
        the life of the daemon process — that's the whole point.
    """

    def __init__(
        self,
        *,
        db_path: Path | None = None,
        socket_path: Path | None = None,
        cost_meter: CostMeter | None = None,
    ) -> None:
        self._db_path: Path = db_path or (default_data_dir() / "tm.db")
        self._socket_path: Path = socket_path or (
            default_data_dir() / DEFAULT_SOCKET_NAME
        )

        # Apply migrations on construct (idempotent; cheap for already-applied).
        # T-FND-07 owns the migration runner; we just call its public method.
        store = SQLiteStore(self._db_path)
        try:
            store.apply_pending_migrations()
        finally:
            store.close()

        # Exactly ONE CostMeter per daemon — soft-alarm flag is per-instance.
        self._cost_meter: CostMeter = cost_meter or CostMeter(self._db_path)

        # Repositories use per-call sqlite3.connect themselves; building them
        # once is purely a convenience.
        self._events = EventsRepository(self._db_path)
        self._goals = GoalsRepository(self._db_path)
        self._vocab = VocabularyRepository(self._db_path)
        self._telemetry = SuggestionTelemetryRepository(self._db_path)

        # Concurrency primitives.
        self._lock = threading.Lock()
        self._shutdown = threading.Event()
        self._listening_sock: socket.socket | None = None
        self._workers: list[threading.Thread] = []
        self._workers_lock = threading.Lock()

        # Method dispatch table — populated lazily so subclasses (if any) can
        # override individual handlers without re-listing them all.
        self._handlers: dict[str, Callable[[dict[str, Any]], Any]] = {
            "ping": self._handle_ping,
            "append_event": self._handle_append_event,
            "add_goal": self._handle_add_goal,
            "complete_goal": self._handle_complete_goal,
            "abandon_goal": self._handle_abandon_goal,
            "add_alias": self._handle_add_alias,
            "add_canonical": self._handle_add_canonical,
            "archive_vocabulary": self._handle_archive_vocabulary,
            "log_suggestion": self._handle_log_suggestion,
            "record_actual_outcome": self._handle_record_actual_outcome,
            "record_thumbs": self._handle_record_thumbs,
            "rebuild_kuzu_projection": self._handle_rebuild_kuzu_projection,
            "check_budget": self._handle_check_budget,
            "record_cost": self._handle_record_cost,
            "run_debrief": self._handle_run_debrief,
            "propose_suggestion": self._handle_propose_suggestion,
        }

    # --------------------------------------------------------------- accessors

    @property
    def db_path(self) -> Path:
        """Read-only path to the SQLite database file."""
        return self._db_path

    @property
    def socket_path(self) -> Path:
        """Read-only path to the Unix domain socket."""
        return self._socket_path

    @property
    def cost_meter(self) -> CostMeter:
        """Return the daemon's singleton :class:`CostMeter`."""
        return self._cost_meter

    @property
    def is_shutdown(self) -> bool:
        """``True`` once :meth:`shutdown` has been called."""
        return self._shutdown.is_set()

    # ------------------------------------------------------------- public API

    def run(self) -> None:
        """Listen on the Unix socket until :meth:`shutdown` is called.

        Blocking.  Intended to be called either from the main thread of a
        ``tm daemon start`` CLI (future) or from a worker thread in tests.
        """
        sock = self._bind_socket()
        self._listening_sock = sock
        try:
            while not self._shutdown.is_set():
                # ``select`` lets us honor the shutdown event without
                # blocking forever in ``accept``.
                ready, _, _ = select.select([sock], [], [], ACCEPT_LOOP_TIMEOUT_S)
                if not ready:
                    continue
                try:
                    conn, _addr = sock.accept()
                except OSError as err:
                    if self._shutdown.is_set():
                        break
                    if err.errno in (errno.EBADF, errno.EINVAL):
                        # listener was closed under us; drop out of the loop
                        break
                    _log.warning("accept() failed: %s", err)
                    continue

                conn.settimeout(SOCKET_TIMEOUT_S)
                t = threading.Thread(
                    target=self._handle_connection,
                    args=(conn,),
                    name="tm-daemon-worker",
                    daemon=True,
                )
                with self._workers_lock:
                    self._workers.append(t)
                t.start()
        finally:
            self._cleanup_listener()
            self._join_workers()

    def shutdown(self) -> None:
        """Signal the listener to stop.  Idempotent."""
        self._shutdown.set()

    def dispatch(self, req: DaemonRequest) -> DaemonResponse:
        """Route a parsed request to its handler.

        Public for tests that exercise dispatch logic without going through
        the socket path.  Handlers raise standard Python exceptions; this
        method catches them and converts to :class:`DaemonResponse` failures.
        """
        handler = self._handlers.get(req.method)
        if handler is None:
            return DaemonResponse.failure(f"unknown method: {req.method!r}")

        try:
            if req.method in _READ_METHODS or req.method in _LLM_METHODS:
                result = handler(req.params)
            else:
                with self._lock:
                    result = handler(req.params)
            return DaemonResponse.success(result)
        except CostCapExceeded as exc:
            return DaemonResponse.failure(str(exc))
        except (ValueError, TypeError, KeyError) as exc:
            return DaemonResponse.failure(f"{type(exc).__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001 — final safety net
            _log.exception("dispatch: unexpected error in %s", req.method)
            return DaemonResponse.failure(f"internal error: {type(exc).__name__}")

    # --------------------------------------------------------- socket plumbing

    def _bind_socket(self) -> socket.socket:
        """Create + bind + listen on the Unix domain socket.

        Steps:

        1. Ensure the socket's parent directory exists (mode ``0700``).
        2. If a socket file already exists, probe it: if a daemon is alive
           there, raise; otherwise unlink the stale entry.
        3. Bind, ``listen``, and ``chmod 0600``.
        """
        parent = self._socket_path.parent
        parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(parent, 0o700)
        except PermissionError:
            # Best-effort; a shared XDG_DATA_HOME may have wider perms set by
            # the OS already.  Don't make this fatal.
            pass

        if self._socket_path.exists():
            self._handle_existing_socket()

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.bind(str(self._socket_path))
        except OSError:
            sock.close()
            raise
        sock.listen(SOCKET_BACKLOG)
        os.chmod(self._socket_path, 0o600)
        return sock

    def _handle_existing_socket(self) -> None:
        """Probe an existing socket file: alive → raise; stale → unlink."""
        st = self._socket_path.stat()
        if not stat.S_ISSOCK(st.st_mode):
            # Stale regular file (test setup or a previous crash before
            # cleanup); unlink and proceed.
            self._socket_path.unlink()
            return

        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(0.5)
        try:
            probe.connect(str(self._socket_path))
        except (ConnectionRefusedError, FileNotFoundError):
            # Nobody listening — safe to take over.
            try:
                self._socket_path.unlink()
            except FileNotFoundError:
                pass
            return
        except OSError:
            # Anything else (permission, etc.) → conservative unlink attempt
            try:
                self._socket_path.unlink()
            except FileNotFoundError:
                pass
            return
        else:
            probe.close()
            raise RuntimeError(
                f"another tm daemon is already listening on {self._socket_path}"
            )
        finally:
            try:
                probe.close()
            except OSError:
                pass

    def _cleanup_listener(self) -> None:
        if self._listening_sock is not None:
            try:
                self._listening_sock.close()
            except OSError:
                pass
            self._listening_sock = None
        try:
            if self._socket_path.exists():
                self._socket_path.unlink()
        except OSError:
            pass

    def _join_workers(self) -> None:
        with self._workers_lock:
            workers = list(self._workers)
        for t in workers:
            t.join(timeout=WORKER_JOIN_TIMEOUT_S)

    # -------------------------------------------------------- per-conn worker

    def _handle_connection(self, conn: socket.socket) -> None:
        """Read line-delimited JSON requests until the peer closes or times out."""
        try:
            with conn, _LineReader(conn, MAX_REQUEST_BYTES) as reader:
                for line in reader:
                    if self._shutdown.is_set() and not line:
                        break
                    response = self._dispatch_line(line)
                    try:
                        conn.sendall(response.to_json())
                    except OSError:
                        break
        except TimeoutError:
            return
        except OSError as err:
            _log.debug("worker OSError: %s", err)
        except Exception:  # noqa: BLE001 — worker must never crash daemon
            _log.exception("worker thread crashed")

    def _dispatch_line(self, line: bytes) -> DaemonResponse:
        try:
            req = DaemonRequest.from_json(line)
        except (json.JSONDecodeError, ValueError) as exc:
            return DaemonResponse.failure(f"bad request: {exc}")
        return self.dispatch(req)

    # =================================================================== handlers

    def _handle_ping(self, params: dict[str, Any]) -> Any:
        """Cheap health check.  Echoes ``params`` back."""
        return {"pong": True, "params": params}

    # ----- events -----

    def _handle_append_event(self, params: dict[str, Any]) -> Any:
        self._events.append_event(**params)
        return {"appended": True}

    # ----- goals -----

    def _handle_add_goal(self, params: dict[str, Any]) -> Any:
        # ``target_completion_at`` arrives as a string over the wire; the
        # repository expects a datetime.  We deliberately do NOT auto-convert
        # — let the caller pass an ISO string that the repository can parse,
        # or omit the field.  Coerce only when present.
        tca = params.get("target_completion_at")
        if isinstance(tca, str):
            from datetime import datetime

            params = dict(params)
            params["target_completion_at"] = datetime.fromisoformat(
                tca.replace("Z", "+00:00")
            )
        goal = self._goals.add(**params)
        return _goal_to_dict(goal)

    def _handle_complete_goal(self, params: dict[str, Any]) -> Any:
        goal = self._goals.complete(**params)
        return _goal_to_dict(goal)

    def _handle_abandon_goal(self, params: dict[str, Any]) -> Any:
        goal = self._goals.abandon(**params)
        return _goal_to_dict(goal)

    # ----- vocabulary -----

    def _handle_add_alias(self, params: dict[str, Any]) -> Any:
        self._vocab.add_alias(**params)
        return {"added": True}

    def _handle_add_canonical(self, params: dict[str, Any]) -> Any:
        entry = self._vocab.add_canonical(**params)
        return {
            "activity_name": entry.activity_name,
            "description": entry.description,
            "vocab_version": entry.vocab_version,
            "added_at": entry.added_at,
            "status": entry.status,
        }

    def _handle_archive_vocabulary(self, params: dict[str, Any]) -> Any:
        self._vocab.archive(**params)
        return {"archived": True}

    # ----- telemetry -----

    def _handle_log_suggestion(self, params: dict[str, Any]) -> Any:
        rec = self._telemetry.log_suggestion(**params)
        return _suggestion_to_dict(rec)

    def _handle_record_actual_outcome(self, params: dict[str, Any]) -> Any:
        self._telemetry.record_actual_outcome(**params)
        return {"recorded": True}

    def _handle_record_thumbs(self, params: dict[str, Any]) -> Any:
        self._telemetry.record_thumbs(**params)
        return {"recorded": True}

    # ----- Kuzu projection -----

    def _handle_rebuild_kuzu_projection(self, params: dict[str, Any]) -> Any:
        """Operator-triggered rebuild of the Kuzu process-model projection."""
        try:
            lens = _projection_lens(params.get("lens"))
            since = _optional_iso_date(params.get("since"), key="since")
            until = _optional_iso_date(params.get("until"), key="until")
            kuzu_db_path = _required_path_string(
                params.get("kuzu_db_path"), key="kuzu_db_path"
            )

            events_repo = EventsRepository(self._db_path)
            process_miner = ProcessMiner(events_repo)
            kuzu_store: KuzuStore | None = None
            try:
                kuzu_store = KuzuStore(kuzu_db_path)
                persisted = rebuild_kuzu_projection(
                    events_repo=events_repo,
                    kuzu_store=kuzu_store,
                    process_miner=process_miner,
                    lens=lens,
                    since=since,
                    until=until,
                )
                if persisted.case_count == 0:
                    kuzu_store.delete_model(persisted.model_id)
                    return {"ok": True, "model_id": None, "skipped": "empty_log"}

                net = kuzu_store.get_petri_net(persisted.model_id)
                if net is None:
                    raise RuntimeError(
                        "persisted Kuzu model was not readable after rebuild"
                    )
                return {
                    "ok": True,
                    "model_id": persisted.model_id,
                    "lens": persisted.lens,
                    "since": persisted.since,
                    "until": persisted.until,
                    "place_count": len(net.places),
                    "transition_count": len(net.transitions),
                    "arc_count": len(net.arcs),
                    "case_event_count": _count_projection_case_events(
                        events_repo=events_repo,
                        lens=lens,
                        since=since,
                        until=until,
                    ),
                }
            finally:
                if kuzu_store is not None:
                    kuzu_store.close()
        except Exception as exc:  # noqa: BLE001 - RPC contract returns structured errors
            return {
                "ok": False,
                "error": type(exc).__name__,
                "detail": str(exc),
            }

    # ----- cost meter -----

    def _handle_check_budget(self, params: dict[str, Any]) -> Any:
        """Pre-call budget gate.  Read-only against the cost ledger."""
        estimated = float(params["estimated_cost_usd"])
        self._cost_meter.check_budget(estimated)
        return {"ok": True, "estimated_cost_usd": estimated}

    def _handle_record_cost(self, params: dict[str, Any]) -> Any:
        cost = self._cost_meter.record(
            model=str(params["model"]),
            input_tokens=int(params["input_tokens"]),
            output_tokens=int(params["output_tokens"]),
            request_kind=str(params["request_kind"]),
        )
        return {"est_cost_usd": cost}

    # ----- LLM-backed agents (debrief + scheduler) -----

    @_llm_envelope
    def _handle_run_debrief(self, params: dict[str, Any]) -> dict[str, Any]:
        """Run a one-shot debrief extraction without spinning up a new process.

        Constructs a per-call :class:`DebriefAgent` (matching the existing
        per-call pattern used elsewhere in the daemon) but reuses the daemon's
        long-lived :class:`CostMeter` singleton so soft-alarm semantics stay
        consistent across requests. The TM_LLM_API_KEY check happens AT REQUEST
        TIME (in the ``@_llm_envelope`` decorator) — the daemon must continue
        to start fine without the key set.
        """
        # Local imports keep daemon module load cheap when these handlers are
        # never called.
        from tm.agents.debrief import DebriefAgent

        transcript = params.get("transcript")
        if not isinstance(transcript, str) or not transcript.strip():
            raise ValueError("transcript must be a non-empty string")

        case_date_raw = params.get("case_date")
        if not isinstance(case_date_raw, str) or not case_date_raw:
            raise ValueError("case_date must be a non-empty string")

        model = params.get("model")
        model_str = str(model) if isinstance(model, str) and model else None
        max_tokens = params.get("max_tokens")
        max_tokens_int = int(max_tokens) if isinstance(max_tokens, int) else None

        # Build LLM client + agent dependencies per-request. The factory
        # is module-level so tests can patch it cleanly.
        llm = _build_llm_client(
            model=model_str or "claude-sonnet-4-6",
            max_tokens=max_tokens_int or 4096,
        )

        vocab_repo = VocabularyRepository(self._db_path)
        events_repo = EventsRepository(self._db_path)
        goals_repo = GoalsRepository(self._db_path)
        aligner = VocabAligner(vocab_repo, llm)

        agent_kwargs: dict[str, Any] = {
            "llm_client": llm,
            "vocab_aligner": aligner,
            "goals_repo": goals_repo,
            "events_repo": events_repo,
            "cost_meter": self._cost_meter,
        }
        if model_str is not None:
            agent_kwargs["model"] = model_str
        if max_tokens_int is not None:
            agent_kwargs["max_tokens"] = max_tokens_int

        agent = DebriefAgent(**agent_kwargs)
        result = agent.extract_and_persist(
            transcript=transcript,
            case_date=case_date_raw,
        )
        return {
            "ok": True,
            "case_date": result.case_date,
            "events_persisted": result.events_persisted,
            "summary_event_persisted": result.summary_event_persisted,
            "novel_labels": list(result.novel_labels),
            "summary": dict(result.summary),
            "cost_estimated_usd": result.cost_estimated_usd,
            "cost_actual_usd": result.cost_actual_usd,
            "extractor_version": result.extractor_version,
            "extracted_at": result.extracted_at,
        }

    @_llm_envelope
    def _handle_propose_suggestion(self, params: dict[str, Any]) -> dict[str, Any]:
        """Run one scheduler propose-suggestion cycle without a new process.

        Mirrors :meth:`_handle_run_debrief`'s per-call construction pattern but
        wires the scheduler agent's full dependency tree. Returns either a
        ``kind="suggestion"`` payload or a ``kind="skipped"`` payload.
        """
        from tm.agents.scheduler import (
            ScheduledSuggestion,
            SchedulerAgent,
            SchedulerSkipReason,
        )

        case_date_raw = params.get("case_date")
        if not isinstance(case_date_raw, str) or not case_date_raw:
            raise ValueError("case_date must be a non-empty string")

        case_goal_id_raw = params.get("case_goal_id")
        if case_goal_id_raw is not None and not isinstance(case_goal_id_raw, str):
            raise ValueError("case_goal_id must be a string or null")
        case_goal_id = case_goal_id_raw or None

        model = params.get("model")
        model_str = str(model) if isinstance(model, str) and model else None
        max_per_day_raw = params.get("max_per_day")
        max_per_day = int(max_per_day_raw) if isinstance(max_per_day_raw, int) else None

        llm = _build_llm_client(
            model=model_str or "claude-sonnet-4-6",
            max_tokens=1024,
        )

        events_repo = EventsRepository(self._db_path)
        goals_repo = GoalsRepository(self._db_path)
        telemetry_repo = SuggestionTelemetryRepository(self._db_path)
        outcome_aggregator = OutcomeAggregator(events_repo)
        process_miner = ProcessMiner(events_repo)
        variant_clusterer = VariantClusterer(events_repo, outcome_aggregator)

        # SchedulerAgent's max_tokens is intentionally not exposed over the
        # wire (unlike run_debrief): scheduler outputs are bounded by the
        # one-suggestion-per-call contract, so the agent's internal default
        # is the right knob. If callers ever need to override it, expose
        # max_tokens here and mirror the debrief handler's pattern.
        agent_kwargs: dict[str, Any] = {
            "llm_client": llm,
            "process_miner": process_miner,
            "variant_clusterer": variant_clusterer,
            "outcome_aggregator": outcome_aggregator,
            "telemetry_repo": telemetry_repo,
            "events_repo": events_repo,
            "goals_repo": goals_repo,
            "cost_meter": self._cost_meter,
            "max_proactive_per_day": max_per_day,
        }
        if model_str is not None:
            agent_kwargs["model"] = model_str

        agent = SchedulerAgent(**agent_kwargs)
        outcome = agent.propose_suggestion(
            case_date=case_date_raw,
            case_goal_id=case_goal_id,
        )

        if isinstance(outcome, SchedulerSkipReason):
            return {
                "ok": True,
                "kind": "skipped",
                "reason": outcome.reason,
                "detail": outcome.detail,
            }

        assert isinstance(outcome, ScheduledSuggestion)  # narrow for mypy
        candidate = outcome.candidate
        return {
            "ok": True,
            "kind": "suggestion",
            "suggestion_id": outcome.suggestion_id,
            "case_date": outcome.case_date,
            "case_goal_id": outcome.case_goal_id,
            "recommended_action": candidate.recommended_action,
            "predicted_outcome_with": float(candidate.predicted_outcome_with),
            "predicted_outcome_without": float(candidate.predicted_outcome_without),
            "predicted_outcome_delta": float(
                outcome.guardrails.predicted_outcome_delta
            ),
            "conformance_deviation": (
                None
                if candidate.predicted_post_suggestion_fitness is None
                else 1.0 - float(candidate.predicted_post_suggestion_fitness)
            ),
            "llm_explanation_text": candidate.explanation,
            "suggested_at": outcome.suggested_at,
            "scheduler_version": outcome.scheduler_version,
        }


# ---------------------------------------------------------------------- helpers


def _goal_to_dict(goal: Any) -> dict[str, Any]:
    """Convert a :class:`tm.models.goals.Goal` to a JSON-friendly dict."""
    return {
        "goal_id": goal.goal_id,
        "name": goal.name,
        "description": goal.description,
        "status": goal.status,
        "priority": goal.priority,
        "target_completion_at": goal.target_completion_at,
        "created_at": goal.created_at,
        "completed_at": goal.completed_at,
        "abandoned_at": goal.abandoned_at,
        "abandon_reason": goal.abandon_reason,
    }


def _suggestion_to_dict(rec: Any) -> dict[str, Any]:
    return {
        "suggestion_id": rec.suggestion_id,
        "suggested_at": rec.suggested_at,
        "case_date": rec.case_date,
        "case_goal_id": rec.case_goal_id,
        "recommended_action": rec.recommended_action,
        "predicted_outcome_with": rec.predicted_outcome_with,
        "predicted_outcome_without": rec.predicted_outcome_without,
        "predicted_outcome_delta": rec.predicted_outcome_delta,
        "conformance_deviation": rec.conformance_deviation,
        "actual_outcome": rec.actual_outcome,
        "explicit_thumbs": rec.explicit_thumbs,
        "llm_explanation_text": rec.llm_explanation_text,
        "created_at": rec.created_at,
    }


def _projection_lens(value: Any) -> str:
    if value not in ("workday", "goal_pursuit"):
        raise ValueError("lens must be one of: workday, goal_pursuit")
    return str(value)


def _optional_iso_date(value: Any, *, key: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be an ISO date string or null")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{key} must be an ISO date string: {value!r}") from exc
    return value


def _required_path_string(value: Any, *, key: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty path string")
    return value


def _count_projection_case_events(
    *,
    events_repo: EventsRepository,
    lens: str,
    since: str | None,
    until: str | None,
) -> int:
    events = events_repo.query_events(since=since, until=until)
    if lens == "workday":
        return sum(
            1
            for event in events
            if event.get("case_date") and event.get("activity") != "debrief_summary"
        )
    return sum(
        1
        for event in events
        if event.get("case_goal_id") and event.get("activity") != "debrief_summary"
    )


# --------------------------------------------------------------------- LineReader


class _LineReader:
    """Iterate over newline-delimited byte lines from a socket.

    Bounds each line by ``max_bytes`` to defend against unbounded input.  The
    iterator yields lines without the trailing ``\\n``; an empty bytes
    indicates EOF and ends iteration.
    """

    def __init__(self, sock: socket.socket, max_bytes: int) -> None:
        self._sock = sock
        self._max = max_bytes
        self._buf = bytearray()

    def __enter__(self) -> _LineReader:
        return self

    def __exit__(self, *_exc: Any) -> None:
        # Connection lifetime is owned by the caller; nothing to do here.
        return None

    def __iter__(self) -> Iterator[bytes]:
        return self

    def __next__(self) -> bytes:
        while True:
            nl = self._buf.find(b"\n")
            if nl != -1:
                line = bytes(self._buf[:nl])
                del self._buf[: nl + 1]
                return line
            if len(self._buf) > self._max:
                raise ValueError(
                    f"request exceeds max_bytes={self._max} without newline"
                )
            chunk = self._sock.recv(4096)
            if not chunk:
                if self._buf:
                    # Trailing line without newline — ignore (we require LF).
                    self._buf.clear()
                raise StopIteration
            self._buf.extend(chunk)


# ============================================================ DaemonClient


class DaemonClient:
    """Convenience client over a daemon's Unix socket.

    Used by tests and intended for CLI / future bot callers.  Each
    :meth:`call` opens a fresh connection (cheap on Unix sockets) so the
    client itself is stateless and thread-safe.

    Parameters
    ----------
    socket_path:
        Path to the daemon's listening socket.
    timeout:
        Per-call socket timeout in seconds.  Defaults to 5 seconds — tests
        should fail fast if the daemon hangs.
    """

    def __init__(self, socket_path: Path | str, *, timeout: float = 5.0) -> None:
        self._socket_path = str(socket_path)
        self._timeout = float(timeout)

    def call(self, method: str, **params: Any) -> Any:
        """Send a request, return ``result`` on success, raise on failure.

        Raises
        ------
        RuntimeError
            On ``ok=False`` responses (carrying the daemon's ``error`` field
            as the message).
        ConnectionError
            If the socket cannot be opened.
        """
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self._timeout)
        try:
            try:
                sock.connect(self._socket_path)
            except (ConnectionRefusedError, FileNotFoundError) as exc:
                raise ConnectionError(
                    f"could not connect to daemon at {self._socket_path}: {exc}"
                ) from exc

            payload = json.dumps({"method": method, "params": params}, default=str)
            sock.sendall((payload + "\n").encode("utf-8"))

            buf = bytearray()
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf.extend(chunk)
                if b"\n" in buf:
                    break
            line = bytes(buf).split(b"\n", 1)[0]
            if not line:
                raise RuntimeError("daemon closed connection without responding")
            obj = json.loads(line.decode("utf-8"))
        finally:
            try:
                sock.close()
            except OSError:
                pass

        if not obj.get("ok"):
            raise RuntimeError(obj.get("error") or "daemon returned ok=False")
        return obj.get("result")


# ----------------------------------------------------------------- run-context


@contextmanager
def _running_daemon(daemon: TMDaemon) -> Iterator[TMDaemon]:
    """Helper for tests: start ``daemon.run()`` in a thread, ensure cleanup.

    Not exported as part of the public API; tests import it explicitly.
    """
    t = threading.Thread(target=daemon.run, name="tm-daemon-runner", daemon=True)
    t.start()
    try:
        # Wait briefly for the listener to be ready by polling the socket
        # path.  Bounded by a small timeout — if the daemon fails to bind,
        # tests should surface the error fast.
        deadline = 2.0
        step = 0.01
        waited = 0.0
        while waited < deadline:
            if daemon.socket_path.exists():
                break
            threading.Event().wait(step)
            waited += step
        yield daemon
    finally:
        daemon.shutdown()
        t.join(timeout=WORKER_JOIN_TIMEOUT_S)
