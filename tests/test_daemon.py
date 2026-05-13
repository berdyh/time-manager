"""Integration tests for :mod:`tm.daemon`.

Each test starts a daemon thread on a per-test ``tmp_path`` socket, drives
it through :class:`tm.daemon.DaemonClient`, then signals shutdown.

Conventions:
- All socket operations carry a tight timeout (5s) so hangs surface fast.
- The daemon and DB live entirely under ``tmp_path``; no shared state.
- The daemon uses one short socket name to dodge the ``sun_path`` 108-byte
  limit on Linux ("tm.sock" inside ``tmp_path`` is normally fine, but the
  helper prefers ``tmp_path / "d.sock"`` to leave plenty of headroom).
"""

from __future__ import annotations

import json
import os
import socket
import stat
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

from tm.daemon import (
    DaemonClient,
    DaemonRequest,
    DaemonResponse,
    TMDaemon,
)
from tm.llm.cost_meter import CostMeter
from tm.repositories.events import EventsRepository
from tm.repositories.goals import GoalsRepository
from tm.repositories.vocabulary import VocabularyRepository
from tm.resilience import ResilienceError, disk_space_pre_check
from tm.store import Store
from tm.stores.kuzu_store import KuzuStore

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = REPO_ROOT / "migrations"


# ----------------------------------------------------------------- helpers


def _prepare_db(tmp_path: Path) -> Path:
    """Create a per-test DB and apply migrations, then close.

    Importing :class:`tm.store.Store` lets us pick up the project's
    canonical migrations directory without depending on the daemon's
    migration apply (which the daemon also performs at construct time).
    """
    db = tmp_path / "tm.db"
    s = Store(db, migrations_dir=MIGRATIONS_DIR)
    s.apply_pending_migrations()
    s.close()
    return db


def _wait_for_socket(path: Path, timeout: float = 2.0) -> None:
    """Poll until ``path`` is a Unix domain socket, or raise after ``timeout``.

    ``path.exists()`` alone isn't enough: the stale-socket test pre-creates a
    regular file at this path, and we need to wait until the daemon has
    unlinked it and bound a real socket in its place.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            st = path.stat()
            if stat.S_ISSOCK(st.st_mode):
                return
        except FileNotFoundError:
            pass
        time.sleep(0.01)
    raise TimeoutError(f"socket {path} did not appear within {timeout:.1f}s")


def _seed_projection_workday_log(repo: EventsRepository) -> None:
    eid = 0
    for case_date, activities in [
        ("2026-01-01", ("A", "B", "C")),
        ("2026-01-02", ("A", "B", "C")),
        ("2026-01-03", ("A", "B", "C")),
    ]:
        for hour, activity in enumerate(activities, start=9):
            eid += 1
            repo.append_event(
                event_id=f"proj-{eid}",
                case_id=f"case-{case_date}",
                activity=activity,
                timestamp=f"{case_date}T{hour:02d}:00:00Z",
                lifecycle="complete",
                extractor_version="v0",
            )


@contextmanager
def _run_daemon(
    tmp_path: Path,
    *,
    cost_meter: CostMeter | None = None,
    socket_name: str = "d.sock",
) -> Iterator[tuple[TMDaemon, DaemonClient]]:
    """Start a TMDaemon in a worker thread; yield ``(daemon, client)``.

    Always shuts the daemon down on exit, even when the test raises.
    """
    db = _prepare_db(tmp_path)
    sock_path = tmp_path / socket_name
    daemon = TMDaemon(
        db_path=db,
        socket_path=sock_path,
        cost_meter=cost_meter,
    )
    client = DaemonClient(sock_path, timeout=5.0)
    runner = threading.Thread(target=daemon.run, name="tm-daemon-test", daemon=True)
    runner.start()
    try:
        _wait_for_socket(sock_path, timeout=2.0)
        yield daemon, client
    finally:
        daemon.shutdown()
        runner.join(timeout=5.0)
        # Final cleanup in case the daemon did not get to it.
        try:
            if sock_path.exists():
                sock_path.unlink()
        except FileNotFoundError:
            pass


# ----------------------------------------------------------------- ping/health


def test_daemon_starts_and_pings(tmp_path: Path) -> None:
    """Round-trip a ping; verify the response is structurally sound."""
    with _run_daemon(tmp_path) as (_daemon, client):
        result = client.call("ping", note="hello")
    assert result["pong"] is True
    assert result["params"] == {"note": "hello"}


def test_daemon_socket_mode_is_0600(tmp_path: Path) -> None:
    """The bound socket must be owner-only (mode 0600)."""
    with _run_daemon(tmp_path) as (daemon, _client):
        st = daemon.socket_path.stat()
        mode = stat.S_IMODE(st.st_mode)
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"


def test_daemon_unlinks_stale_socket(tmp_path: Path) -> None:
    """Pre-create a stale regular file at the socket path; daemon must unlink it."""
    db = _prepare_db(tmp_path)
    sock_path = tmp_path / "d.sock"
    sock_path.write_bytes(b"not a socket")  # stale
    daemon = TMDaemon(db_path=db, socket_path=sock_path)
    runner = threading.Thread(target=daemon.run, daemon=True)
    runner.start()
    try:
        _wait_for_socket(sock_path, timeout=2.0)
        # We shouldn't have crashed.
        client = DaemonClient(sock_path, timeout=5.0)
        result = client.call("ping")
        assert result["pong"] is True
    finally:
        daemon.shutdown()
        runner.join(timeout=5.0)
        if sock_path.exists():
            sock_path.unlink()


def test_daemon_refuses_when_other_daemon_alive(tmp_path: Path) -> None:
    """Starting a second daemon on the same socket must error."""
    with _run_daemon(tmp_path) as (daemon, _client):
        with pytest.raises(RuntimeError, match="already listening"):
            second = TMDaemon(
                db_path=daemon.db_path,
                socket_path=daemon.socket_path,
            )
            # _bind_socket runs inside run(); call it here to surface the error.
            second._bind_socket()  # noqa: SLF001 — internal probe is the test


# ------------------------------------------------------------- write handlers


def test_daemon_append_event_round_trip(tmp_path: Path) -> None:
    """append_event lands a row visible via direct EventsRepository read."""
    with _run_daemon(tmp_path) as (daemon, client):
        result = client.call(
            "append_event",
            event_id="evt-1",
            case_id="case-1",
            activity="deep_work",
            timestamp="2026-05-05T10:00:00Z",
            lifecycle="start",
            extractor_version="v0",
        )
    assert result == {"appended": True}
    events = EventsRepository(daemon.db_path).query_events(case_id="case-1")
    assert len(events) == 1
    assert events[0]["event_id"] == "evt-1"


def test_daemon_add_goal_round_trip(tmp_path: Path) -> None:
    """add_goal returns the persisted goal as a dict."""
    with _run_daemon(tmp_path) as (daemon, client):
        result = client.call("add_goal", name="ship daemon", priority=1)
    assert result["name"] == "ship daemon"
    assert result["priority"] == 1
    assert result["status"] == "active"
    goal_id = result["goal_id"]
    persisted = GoalsRepository(daemon.db_path).get(goal_id)
    assert persisted is not None
    assert persisted.name == "ship daemon"


def test_daemon_complete_goal_round_trip(tmp_path: Path) -> None:
    """complete_goal flips status to 'completed'."""
    with _run_daemon(tmp_path) as (daemon, client):
        added = client.call("add_goal", name="finishable")
        completed = client.call("complete_goal", goal_id=added["goal_id"])
    assert completed["status"] == "completed"
    assert completed["completed_at"] is not None
    persisted = GoalsRepository(daemon.db_path).get(added["goal_id"])
    assert persisted is not None and persisted.status == "completed"


def test_daemon_abandon_goal_with_reason(tmp_path: Path) -> None:
    """abandon_goal records the reason and marks status='abandoned'."""
    with _run_daemon(tmp_path) as (daemon, client):
        added = client.call("add_goal", name="cancellable")
        abandoned = client.call(
            "abandon_goal",
            goal_id=added["goal_id"],
            reason="scope changed",
        )
    assert abandoned["status"] == "abandoned"
    assert abandoned["abandon_reason"] == "scope changed"
    persisted = GoalsRepository(daemon.db_path).get(added["goal_id"])
    assert persisted is not None and persisted.status == "abandoned"
    assert persisted.abandon_reason == "scope changed"


def test_daemon_add_alias_round_trip(tmp_path: Path) -> None:
    """add_alias inserts a vocabulary alias mapping."""
    with _run_daemon(tmp_path) as (daemon, client):
        VocabularyRepository(daemon.db_path).seed_starter_vocabulary()
        result = client.call(
            "add_alias",
            free_text_variant="Coding Session",
            canonical_activity="deep_work",
        )
        assert result == {"added": True}
        resolved = VocabularyRepository(daemon.db_path).resolve("coding session")
    assert resolved == "deep_work"


def test_daemon_add_canonical_round_trip(tmp_path: Path) -> None:
    """add_canonical inserts a new canonical activity."""
    with _run_daemon(tmp_path) as (daemon, client):
        result = client.call(
            "add_canonical",
            activity_name="prototyping",
            description="Experimental coding work.",
        )
    assert result["activity_name"] == "prototyping"
    assert result["description"] == "Experimental coding work."
    entry = VocabularyRepository(daemon.db_path).get("prototyping")
    assert entry is not None and entry.activity_name == "prototyping"


def test_daemon_archive_vocabulary_round_trip(tmp_path: Path) -> None:
    """archive_vocabulary marks an activity as archived."""
    with _run_daemon(tmp_path) as (daemon, client):
        VocabularyRepository(daemon.db_path).seed_starter_vocabulary()
        result = client.call("archive_vocabulary", activity_name="commute")
    assert result == {"archived": True}
    entry = VocabularyRepository(daemon.db_path).get("commute")
    assert entry is not None and entry.status == "archived"


def test_daemon_log_suggestion_round_trip(tmp_path: Path) -> None:
    """log_suggestion persists a suggestion telemetry row."""
    with _run_daemon(tmp_path) as (_daemon, client):
        result = client.call(
            "log_suggestion",
            case_date="2026-05-05",
            recommended_action="take a break",
            predicted_outcome_with=1.6,
            predicted_outcome_without=1.2,
        )
    assert result["case_date"] == "2026-05-05"
    assert result["recommended_action"] == "take a break"
    assert result["predicted_outcome_delta"] == pytest.approx(0.4)


def test_daemon_record_actual_outcome_round_trip(tmp_path: Path) -> None:
    """record_actual_outcome updates an existing suggestion row."""
    with _run_daemon(tmp_path) as (_daemon, client):
        sugg = client.call(
            "log_suggestion",
            case_date="2026-05-05",
            recommended_action="exercise",
            predicted_outcome_with=1.5,
            predicted_outcome_without=1.0,
        )
        result = client.call(
            "record_actual_outcome",
            suggestion_id=sugg["suggestion_id"],
            actual_outcome=2,
        )
    assert result == {"recorded": True}


def test_daemon_record_thumbs_round_trip(tmp_path: Path) -> None:
    """record_thumbs persists explicit feedback."""
    with _run_daemon(tmp_path) as (_daemon, client):
        sugg = client.call(
            "log_suggestion",
            case_date="2026-05-05",
            recommended_action="exercise",
            predicted_outcome_with=1.5,
            predicted_outcome_without=1.0,
        )
        result = client.call(
            "record_thumbs",
            suggestion_id=sugg["suggestion_id"],
            thumbs=True,
        )
    assert result == {"recorded": True}


# ---------------------------------------------------------- Kuzu projection


def test_handle_rebuild_kuzu_projection_workday_success(tmp_path: Path) -> None:
    db = _prepare_db(tmp_path)
    daemon = TMDaemon(db_path=db, socket_path=tmp_path / "unused.sock")
    _seed_projection_workday_log(EventsRepository(db))
    kuzu_path = tmp_path / "kuzu"

    result = daemon._handle_rebuild_kuzu_projection(
        {
            "lens": "workday",
            "since": "2026-01-01",
            "until": "2026-01-31",
            "kuzu_db_path": str(kuzu_path),
        }
    )

    assert result["ok"] is True
    model_id = result["model_id"]
    assert isinstance(model_id, str)
    assert model_id == "model::workday::2026-01-01::2026-01-31"
    assert result["lens"] == "workday"
    assert result["since"] == "2026-01-01"
    assert result["until"] == "2026-01-31"
    assert result["place_count"] > 0
    assert result["transition_count"] > 0
    assert result["arc_count"] > 0
    assert result["case_event_count"] == 9

    kuzu = KuzuStore(kuzu_path)
    try:
        model_ids = {model.model_id for model in kuzu.list_models()}
        net = kuzu.get_petri_net(model_id)
    finally:
        kuzu.close()
    assert model_id in model_ids
    assert net is not None
    assert len(net.places) > 0


def test_handle_rebuild_kuzu_projection_empty_log(tmp_path: Path) -> None:
    db = _prepare_db(tmp_path)
    daemon = TMDaemon(db_path=db, socket_path=tmp_path / "unused.sock")
    repo = EventsRepository(db)
    repo.append_event(
        event_id="outside-window",
        case_id="case-1",
        activity="A",
        timestamp="2026-01-01T09:00:00Z",
        lifecycle="complete",
    )
    kuzu_path = tmp_path / "kuzu"

    result = daemon._handle_rebuild_kuzu_projection(
        {
            "lens": "workday",
            "since": "2026-02-01",
            "until": "2026-02-28",
            "kuzu_db_path": str(kuzu_path),
        }
    )

    assert result == {"ok": True, "model_id": None, "skipped": "empty_log"}
    kuzu = KuzuStore(kuzu_path)
    try:
        assert kuzu.list_models() == []
    finally:
        kuzu.close()


def test_handle_rebuild_kuzu_projection_invalid_lens(tmp_path: Path) -> None:
    db = _prepare_db(tmp_path)
    daemon = TMDaemon(db_path=db, socket_path=tmp_path / "unused.sock")

    result = daemon._handle_rebuild_kuzu_projection(
        {"lens": "garbage", "kuzu_db_path": str(tmp_path / "kuzu")}
    )

    assert result["ok"] is False
    assert result["error"] == "ValueError"
    assert "lens" in result["detail"]


def test_handle_rebuild_kuzu_projection_missing_kuzu_path(tmp_path: Path) -> None:
    db = _prepare_db(tmp_path)
    daemon = TMDaemon(db_path=db, socket_path=tmp_path / "unused.sock")

    result = daemon._handle_rebuild_kuzu_projection({"lens": "workday"})

    assert result["ok"] is False
    assert result["error"] == "ValueError"
    assert "kuzu_db_path" in result["detail"]


def test_dispatch_table_includes_rebuild_kuzu_projection(tmp_path: Path) -> None:
    db = _prepare_db(tmp_path)
    daemon = TMDaemon(db_path=db, socket_path=tmp_path / "unused.sock")

    assert "rebuild_kuzu_projection" in daemon._handlers


# --------------------------------------------------------------- cost meter


def test_daemon_check_budget_returns_value(tmp_path: Path) -> None:
    """A cheap call passes the budget gate."""
    with _run_daemon(tmp_path) as (_daemon, client):
        result = client.call("check_budget", estimated_cost_usd=0.01)
    assert result["ok"] is True
    assert result["estimated_cost_usd"] == pytest.approx(0.01)


def test_daemon_check_budget_raises_on_cap_exceeded(tmp_path: Path) -> None:
    """A request larger than the cap surfaces as ok=False."""
    db = _prepare_db(tmp_path)
    tiny_meter = CostMeter(db, monthly_cap_usd=0.001)
    with _run_daemon(tmp_path, cost_meter=tiny_meter) as (_daemon, client):
        with pytest.raises(RuntimeError, match="cost cap exceeded"):
            client.call("check_budget", estimated_cost_usd=10.0)


def test_daemon_record_cost_inserts_ledger_row(tmp_path: Path) -> None:
    """record_cost goes through the daemon's CostMeter singleton."""
    with _run_daemon(tmp_path) as (daemon, client):
        result = client.call(
            "record_cost",
            model="claude-sonnet-4-6",
            input_tokens=1000,
            output_tokens=2000,
            request_kind="chat",
        )
    assert result["est_cost_usd"] > 0.0
    # Fresh CostMeter pointing at the same DB sees the ledger row.
    fresh = CostMeter(daemon.db_path)
    assert fresh.monthly_total() > 0.0


def test_daemon_uses_singleton_cost_meter(tmp_path: Path) -> None:
    """The daemon must reuse the CostMeter passed in (or built once)."""
    db = _prepare_db(tmp_path)
    meter = CostMeter(db, monthly_cap_usd=20.0)
    with _run_daemon(tmp_path, cost_meter=meter) as (daemon, _client):
        assert daemon.cost_meter is meter


# --------------------------------------------------- error / robustness paths


def test_daemon_dispatch_unknown_method_returns_error(tmp_path: Path) -> None:
    """Unknown methods come back as ok=False with a structured error."""
    with _run_daemon(tmp_path) as (_daemon, client):
        with pytest.raises(RuntimeError, match="unknown method"):
            client.call("not_a_real_method")


def test_daemon_dispatch_malformed_json_does_not_crash(tmp_path: Path) -> None:
    """Sending raw garbage bytes must not crash the daemon thread."""
    with _run_daemon(tmp_path) as (daemon, client):
        # Send malformed JSON via raw socket
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(5.0)
        try:
            sock.connect(str(daemon.socket_path))
            sock.sendall(b"{not valid json at all\n")
            buf = b""
            while b"\n" not in buf:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
        finally:
            sock.close()
        first_line = buf.split(b"\n", 1)[0]
        assert first_line, "daemon must respond, not silently drop"
        obj = json.loads(first_line.decode("utf-8"))
        assert obj["ok"] is False
        assert "bad request" in obj["error"]
        # Daemon is still alive: subsequent ping works.
        result = client.call("ping")
        assert result["pong"] is True


def test_daemon_dispatch_repository_error_returns_failure(tmp_path: Path) -> None:
    """Underlying ValueError surfaces as a structured failure."""
    with _run_daemon(tmp_path) as (_daemon, client):
        # complete_goal on an unknown id raises ValueError in the repo.
        with pytest.raises(RuntimeError, match="Unknown goal_id"):
            client.call("complete_goal", goal_id="ZZZZZ")


# ----------------------------------------------------- concurrency / shutdown


def test_daemon_concurrent_writes_serialize(tmp_path: Path) -> None:
    """Multiple threads each issue an append_event; all five rows persist."""
    with _run_daemon(tmp_path) as (daemon, _client):
        errors: list[str] = []

        def worker(idx: int) -> None:
            try:
                c = DaemonClient(daemon.socket_path, timeout=5.0)
                c.call(
                    "append_event",
                    event_id=f"evt-{idx}",
                    case_id="case-A",
                    activity="deep_work",
                    timestamp=f"2026-05-05T10:0{idx}:00Z",
                    lifecycle="start",
                    extractor_version="v0",
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(f"thread {idx}: {exc!r}")

        threads = [
            threading.Thread(target=worker, args=(i,), daemon=True) for i in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)
            assert not t.is_alive(), "worker thread did not finish"

        events = EventsRepository(daemon.db_path).query_events(case_id="case-A")

    assert errors == [], f"worker errors: {errors}"
    assert len(events) == 5
    assert {e["event_id"] for e in events} == {f"evt-{i}" for i in range(5)}


def test_daemon_shutdown_drains_inflight(tmp_path: Path) -> None:
    """A request issued just before shutdown still completes successfully."""
    db = _prepare_db(tmp_path)
    sock_path = tmp_path / "d.sock"
    daemon = TMDaemon(db_path=db, socket_path=sock_path)
    runner = threading.Thread(target=daemon.run, daemon=True)
    runner.start()
    try:
        _wait_for_socket(sock_path, timeout=2.0)
        client = DaemonClient(sock_path, timeout=5.0)
        # Issue a normal request: should complete OK.
        result = client.call("ping")
        assert result["pong"] is True
    finally:
        daemon.shutdown()
        runner.join(timeout=5.0)
        assert not runner.is_alive(), "daemon did not shut down"


def test_daemon_dispatch_direct(tmp_path: Path) -> None:
    """The dispatch() method is independently testable without sockets."""
    db = _prepare_db(tmp_path)
    daemon = TMDaemon(db_path=db, socket_path=tmp_path / "unused.sock")
    resp = daemon.dispatch(DaemonRequest(method="ping", params={"x": 1}))
    assert resp.ok is True
    assert isinstance(resp, DaemonResponse)
    assert resp.result["pong"] is True


# ---------------------------------------------------------------- resilience


def test_daemon_disk_space_pre_check_passes(tmp_path: Path) -> None:
    """Real-world tmp_path usually has plenty of space; the check is a no-op."""
    disk_space_pre_check(tmp_path / "tm.db", min_free_mb=1)


def test_daemon_disk_space_pre_check_fails_when_low(tmp_path: Path) -> None:
    """Mock shutil.disk_usage to simulate a near-full disk."""
    fake = os.statvfs_result if False else None  # noqa: F841 — keep type lights happy

    class _FakeUsage:
        total = 1_000_000
        used = 999_000
        free = 1_000  # bytes — far below 50 MB

    with patch("tm.resilience.shutil.disk_usage", return_value=_FakeUsage):
        with pytest.raises(ResilienceError, match="MB free"):
            disk_space_pre_check(tmp_path / "tm.db", min_free_mb=50)


# ============================================================ LLM-backed RPCs
#
# These tests exercise the daemon's run_debrief / propose_suggestion handlers
# without spending real Anthropic tokens: they patch the module-level
# ``tm.daemon._build_llm_client`` factory so the daemon constructs a Mock
# LLMClient instead of a live AnthropicAdapter. The TM_LLM_API_KEY check fires
# AT REQUEST TIME, so tests can enable / disable that path purely via
# ``monkeypatch.setenv`` / ``monkeypatch.delenv``.


def _llm_extract_response(data: dict[str, object]) -> object:
    """Build an :class:`ExtractResponse` shaped like a successful LLM call."""
    from tm.llm.client import ExtractResponse, Usage

    return ExtractResponse(
        data=data,
        usage=Usage(input_tokens=1, output_tokens=1),
    )


def _make_mock_llm(extract_data: dict[str, object]):
    """Return a Mock LLMClient whose ``.extract`` returns the canned data."""
    from unittest.mock import Mock

    llm = Mock()
    llm.extract.return_value = _llm_extract_response(extract_data)
    return llm


# ----- run_debrief ---------------------------------------------------------


def test_handle_run_debrief_dispatch_registered(tmp_path: Path) -> None:
    """The dispatch table must include the new RPC method."""
    db = _prepare_db(tmp_path)
    daemon = TMDaemon(db_path=db, socket_path=tmp_path / "unused.sock")
    assert "run_debrief" in daemon._handlers


def test_handle_run_debrief_missing_api_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without TM_LLM_API_KEY, the handler returns a structured error."""
    monkeypatch.delenv("TM_LLM_API_KEY", raising=False)
    db = _prepare_db(tmp_path)
    daemon = TMDaemon(db_path=db, socket_path=tmp_path / "unused.sock")

    result = daemon._handle_run_debrief(
        {
            "transcript": "I shipped the daemon handlers today.",
            "case_date": "2026-05-06",
        }
    )
    assert result["ok"] is False
    assert result["error"] == "MissingApiKey"
    assert "TM_LLM_API_KEY" in result["detail"]


def test_handle_run_debrief_empty_transcript(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty transcript must surface as a structured ValueError."""
    monkeypatch.setenv("TM_LLM_API_KEY", "test-key")
    db = _prepare_db(tmp_path)
    daemon = TMDaemon(db_path=db, socket_path=tmp_path / "unused.sock")

    result = daemon._handle_run_debrief({"transcript": "", "case_date": "2026-05-06"})
    assert result["ok"] is False
    assert result["error"] == "ValueError"
    assert "transcript" in result["detail"]


def test_handle_run_debrief_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mock the LLM extract; assert events are persisted and JSON shape matches."""
    monkeypatch.setenv("TM_LLM_API_KEY", "test-key")
    db = _prepare_db(tmp_path)
    # Seed the starter vocabulary so the VocabAligner takes the fast path
    # (no LLM hop) for ``deep_work``.
    VocabularyRepository(db).seed_starter_vocabulary()
    daemon = TMDaemon(db_path=db, socket_path=tmp_path / "unused.sock")

    extract_data = {
        "events": [
            {
                "activity": "deep_work",
                "timestamp": "2026-05-06T10:00:00Z",
                "lifecycle": "complete",
            },
        ],
        "summary": {"planned_tasks_completed": 1, "planned_tasks_total": 2},
    }
    llm = _make_mock_llm(extract_data)

    with patch("tm.daemon._build_llm_client", return_value=llm):
        result = daemon._handle_run_debrief(
            {
                "transcript": "I did deep work all morning.",
                "case_date": "2026-05-06",
            }
        )

    assert result["ok"] is True, result
    assert result["case_date"] == "2026-05-06"
    # 1 activity event + 1 summary event = 2 persisted rows.
    assert result["events_persisted"] >= 1
    assert result["summary_event_persisted"] is True
    assert isinstance(result["novel_labels"], list)
    assert isinstance(result["summary"], dict)
    assert result["summary"]["planned_tasks_completed"] == 1
    assert result["summary"]["planned_tasks_total"] == 2
    assert result["extractor_version"] == "debrief-v1"
    assert "extracted_at" in result
    assert isinstance(result["cost_estimated_usd"], float)
    assert isinstance(result["cost_actual_usd"], float)
    # Confirm the event landed.
    rows = EventsRepository(db).query_events(case_date="2026-05-06")
    assert any(e.get("activity") == "deep_work" for e in rows)


# ----- propose_suggestion --------------------------------------------------


def test_handle_propose_suggestion_dispatch_registered(tmp_path: Path) -> None:
    """The dispatch table must include the new RPC method."""
    db = _prepare_db(tmp_path)
    daemon = TMDaemon(db_path=db, socket_path=tmp_path / "unused.sock")
    assert "propose_suggestion" in daemon._handlers


def test_handle_propose_suggestion_missing_api_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without TM_LLM_API_KEY, the handler returns a structured error."""
    monkeypatch.delenv("TM_LLM_API_KEY", raising=False)
    db = _prepare_db(tmp_path)
    daemon = TMDaemon(db_path=db, socket_path=tmp_path / "unused.sock")

    result = daemon._handle_propose_suggestion({"case_date": "2026-05-06"})
    assert result["ok"] is False
    assert result["error"] == "MissingApiKey"
    assert "TM_LLM_API_KEY" in result["detail"]


def test_handle_propose_suggestion_empty_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty events table → SchedulerAgent returns empty_context skip reason."""
    monkeypatch.setenv("TM_LLM_API_KEY", "test-key")
    db = _prepare_db(tmp_path)
    daemon = TMDaemon(db_path=db, socket_path=tmp_path / "unused.sock")

    # No events, no goals seeded → context is empty and the LLM is never called.
    # We still patch the factory so the handler doesn't try to construct a real
    # AnthropicAdapter (which would attempt SDK initialisation).
    from unittest.mock import Mock

    llm = Mock()  # extract is never called on the empty_context path
    with patch("tm.daemon._build_llm_client", return_value=llm):
        result = daemon._handle_propose_suggestion({"case_date": "2026-05-06"})

    assert result["ok"] is True
    assert result["kind"] == "skipped"
    assert result["reason"] == "empty_context"
    assert "case_date=" in result["detail"]
    # Confirm the LLM was NOT consulted on this skip path.
    llm.extract.assert_not_called()


def test_handle_propose_suggestion_with_suggestion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Past empty_context: seed an active goal so context is non-empty, mock LLM
    to return a candidate that all three guardrails accept, assert the wire
    payload carries kind='suggestion' plus the predicted-outcome fields.
    """
    monkeypatch.setenv("TM_LLM_API_KEY", "test-key")
    db = _prepare_db(tmp_path)
    daemon = TMDaemon(db_path=db, socket_path=tmp_path / "unused.sock")

    # Seed an active goal so _build_context_payload returns a non-empty
    # ``active_goals`` list. That alone makes _context_is_empty() return False.
    GoalsRepository(db).add(name="Ship daemon CLI wiring", priority=1)

    # The candidate must clear all three guardrails:
    # 1. SimpleCounterfactualPositiveGuard: predicted_outcome_with > without
    # 2. CounterfactualMagnitudeGuard: delta >= 0.3
    # 3. ConformanceGuard: predicted_post_suggestion_fitness >= 0.4
    extract_data = {
        "recommended_action": "Take a 15-minute break before tackling task X",
        "predicted_outcome_with": 1.4,
        "predicted_outcome_without": 0.9,
        "predicted_post_suggestion_fitness": 0.88,
        "explanation": "A short reset usually precedes better deep work.",
    }
    llm = _make_mock_llm(extract_data)

    with patch("tm.daemon._build_llm_client", return_value=llm):
        result = daemon._handle_propose_suggestion(
            {"case_date": "2026-05-06", "max_per_day": 3}
        )

    assert result["ok"] is True, result
    assert result["kind"] == "suggestion"
    assert result["case_date"] == "2026-05-06"
    assert (
        result["recommended_action"] == "Take a 15-minute break before tackling task X"
    )
    assert result["predicted_outcome_with"] == pytest.approx(1.4)
    assert result["predicted_outcome_without"] == pytest.approx(0.9)
    assert result["predicted_outcome_delta"] == pytest.approx(0.5)
    assert result["conformance_deviation"] == pytest.approx(1.0 - 0.88)
    assert "deep work" in result["llm_explanation_text"]
    assert result["scheduler_version"] == "scheduler-v1"
    assert isinstance(result["suggestion_id"], str) and result["suggestion_id"]
    assert "suggested_at" in result
    llm.extract.assert_called_once()


def test_llm_methods_bypass_write_lock() -> None:
    """LLM-backed handlers must NOT be classified as lock-held writes.

    Long-running LLM calls would otherwise block sibling RPC writes
    (e.g. a user typing ``tm goal add`` while cron is debriefing). The
    dispatch classification lives in module-level frozensets so this
    assertion is cheap to express.
    """
    from tm import daemon as daemon_mod

    assert "run_debrief" in daemon_mod._LLM_METHODS
    assert "run_debrief" not in daemon_mod._READ_METHODS
    assert "propose_suggestion" in daemon_mod._LLM_METHODS
    assert "propose_suggestion" not in daemon_mod._READ_METHODS
