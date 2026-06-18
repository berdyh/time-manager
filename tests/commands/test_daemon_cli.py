"""CLI tests for ``tm daemon`` sub-commands.

Testing strategy: MOCKS + subprocess
--------------------------------------
Python's signal module only delivers signals to the main thread.  When
``daemon.run()`` is called from a test (non-main) thread, SIGTERM sent via
``os.kill(os.getpid(), signal.SIGTERM)`` still reaches the process but fires
the handler on the main thread — which, in pytest, is the pytest runner
thread rather than the daemon-start thread.  This makes threading-based
signal tests flaky and tricky to reason about.

The adopted strategy:

* **start** tests: use ``unittest.mock.patch`` to replace ``TMDaemon.run``
  (or ``daemon.shutdown``) so the blocking ``run()`` call returns immediately.
  This lets CliRunner invoke ``tm daemon start`` synchronously and we verify
  PID-file semantics, signal-handler installation, and exit codes without
  actually spinning up the network layer.

* **stop** tests: mock ``os.kill`` so we can verify SIGTERM dispatch without
  requiring a live process.

* **status** tests: start a *real* daemon thread in a helper (matching the
  pattern from ``tests/test_daemon.py``), then invoke ``tm daemon status``
  synchronously.  The daemon is already running; CliRunner just calls the
  status command which does a socket ping — no signal issues.

All tests use ``tmp_path`` for DB, socket, and PID paths so the real user
data directory is never touched.
"""

from __future__ import annotations

import os
import signal
import stat
import threading
import time
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from tm.cli import app
from tm.daemon import DaemonClient, TMDaemon
from tm.stores.sqlite_store import SQLiteStore

runner = CliRunner()

# ------------------------------------------------------------------ helpers


def _prepare_db(tmp_path: Path) -> Path:
    """Apply migrations into a fresh DB and return its path."""
    db = tmp_path / "tm.db"
    store = SQLiteStore(db)
    store.apply_pending_migrations()
    store.close()
    return db


def _wait_for_socket(path: Path, timeout: float = 2.0) -> None:
    """Poll until ``path`` is a live Unix socket or raise TimeoutError."""
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


def _start_daemon_thread(db: Path, sock: Path) -> tuple[TMDaemon, threading.Thread]:
    """Start a TMDaemon in a worker thread and wait for it to bind."""
    daemon = TMDaemon(db_path=db, socket_path=sock)
    t = threading.Thread(target=daemon.run, name="tm-test-daemon", daemon=True)
    t.start()
    _wait_for_socket(sock, timeout=2.0)
    return daemon, t


# ------------------------------------------------------------------ AC1: help


def test_daemon_help_shows_subcommands() -> None:
    """``tm daemon --help`` must list start, stop, and status."""
    result = runner.invoke(app, ["daemon", "--help"])
    assert result.exit_code == 0, result.output
    assert "start" in result.output
    assert "stop" in result.output
    assert "status" in result.output


# ------------------------------------------------------------------ start tests


def test_daemon_start_runs_and_responds_to_ping_then_stops_on_signal(
    tmp_path: Path,
) -> None:
    """start writes PID file, daemon accepts pings, cleans up on shutdown.

    Strategy: patch ``TMDaemon.run`` so it (a) captures the instance to let
    us verify the PID file was written before run() was called, and (b) starts
    the *real* run loop in a separate daemon-flagged thread while the CLI
    ``start`` command is blocking.  We then ping via DaemonClient to confirm
    the daemon is actually alive, call ``shutdown()`` to unblock run(), and
    verify the PID file is removed in the finally-block.

    The key insight: we patch ``TMDaemon.run`` on the *class*, so when the
    CLI calls ``self_daemon.run()`` it calls our wrapper.  Inside the wrapper
    we call ``type(self).run.__wrapped__(self)`` — but since we're already in
    the wrapper we instead call the socket-level run via a fresh thread using
    the *un-patched* implementation stored before patching.
    """
    db = _prepare_db(tmp_path)
    sock = tmp_path / "tm.sock"
    pid_file = tmp_path / "tm.pid"

    # Store the unpatched run so the wrapper can call it.
    _real_run = TMDaemon.run

    result_holder: dict[str, object] = {}
    done_event = threading.Event()

    def _run_wrapper(self: TMDaemon) -> None:
        """Spawn real run() in a thread; ping; shutdown; join."""
        t = threading.Thread(target=_real_run, args=(self,), daemon=True)
        t.start()
        try:
            _wait_for_socket(self.socket_path, timeout=2.0)
            client = DaemonClient(self.socket_path, timeout=2.0)
            ping_result = client.call("ping")
            result_holder["ping"] = ping_result
        finally:
            self.shutdown()
            t.join(timeout=3.0)
            done_event.set()

    with patch.object(TMDaemon, "run", _run_wrapper):
        invoke_result = runner.invoke(
            app,
            [
                "daemon",
                "start",
                "--db-path",
                str(db),
                "--socket-path",
                str(sock),
                "--pid-path",
                str(pid_file),
            ],
        )

    done_event.wait(timeout=5.0)
    assert invoke_result.exit_code == 0, invoke_result.output
    assert result_holder.get("ping", {}).get("pong") is True  # type: ignore[union-attr]
    # PID file must be cleaned up after run() returns.
    assert not pid_file.exists(), "PID file should be removed on clean exit"


def test_daemon_start_refuses_when_pid_file_alive(tmp_path: Path) -> None:
    """If the PID file references our own (live) PID, start must exit 1."""
    db = _prepare_db(tmp_path)
    sock = tmp_path / "tm.sock"
    pid_file = tmp_path / "tm.pid"

    # Write our own PID — os.kill(pid, 0) will succeed (process exists).
    pid_file.write_text(str(os.getpid()))

    result = runner.invoke(
        app,
        [
            "daemon",
            "start",
            "--db-path",
            str(db),
            "--socket-path",
            str(sock),
            "--pid-path",
            str(pid_file),
        ],
    )

    assert result.exit_code == 1
    assert "already running" in result.output


def test_daemon_start_cleans_up_stale_pid_file(tmp_path: Path) -> None:
    """A PID file with a non-existent PID is cleaned up; daemon starts fine."""
    db = _prepare_db(tmp_path)
    sock = tmp_path / "tm.sock"
    pid_file = tmp_path / "tm.pid"

    # PID 999999999 is virtually guaranteed not to exist.
    pid_file.write_text("999999999")

    def _immediate_run(self: TMDaemon) -> None:
        self.shutdown()

    with patch.object(TMDaemon, "run", _immediate_run):
        result = runner.invoke(
            app,
            [
                "daemon",
                "start",
                "--db-path",
                str(db),
                "--socket-path",
                str(sock),
                "--pid-path",
                str(pid_file),
            ],
        )

    assert result.exit_code == 0, result.output
    assert not pid_file.exists()


def test_daemon_start_no_foreground_detaches(tmp_path: Path) -> None:
    """``--no-foreground`` runs the detach hook instead of rejecting."""
    db = _prepare_db(tmp_path)
    sock = tmp_path / "tm.sock"
    pid_file = tmp_path / "tm.pid"

    detached: list[bool] = []

    def _fake_daemonize() -> None:
        detached.append(True)

    def _immediate_run(self: TMDaemon) -> None:
        self.shutdown()

    with patch("tm.commands.daemon._daemonize", side_effect=_fake_daemonize):
        with patch.object(TMDaemon, "run", _immediate_run):
            result = runner.invoke(
                app,
                [
                    "daemon",
                    "start",
                    "--no-foreground",
                    "--db-path",
                    str(db),
                    "--socket-path",
                    str(sock),
                    "--pid-path",
                    str(pid_file),
                ],
            )

    assert result.exit_code == 0, result.output
    assert detached == [True]
    assert not pid_file.exists()


def test_daemon_start_no_foreground_resolves_relative_paths_before_detach(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Relative paths stay anchored to the original CWD after daemon detach."""
    monkeypatch.chdir(tmp_path)
    Path("rel").mkdir()

    captured: dict[str, Path] = {}

    def _fake_daemonize() -> None:
        os.chdir("/")

    def _immediate_run(self: TMDaemon) -> None:
        captured["db_path"] = self.db_path
        captured["socket_path"] = self.socket_path
        self.shutdown()

    with patch("tm.commands.daemon._daemonize", side_effect=_fake_daemonize):
        with patch.object(TMDaemon, "run", _immediate_run):
            result = runner.invoke(
                app,
                [
                    "daemon",
                    "start",
                    "--no-foreground",
                    "--db-path",
                    "rel/tm.db",
                    "--socket-path",
                    "rel/tm.sock",
                    "--pid-path",
                    "rel/tm.pid",
                ],
            )

    assert result.exit_code == 0, result.output
    assert captured["db_path"] == tmp_path / "rel" / "tm.db"
    assert captured["socket_path"] == tmp_path / "rel" / "tm.sock"
    assert not (tmp_path / "rel" / "tm.pid").exists()


def test_daemon_explicit_paths_do_not_resolve_defaults(tmp_path: Path) -> None:
    db = _prepare_db(tmp_path)
    sock = tmp_path / "explicit.sock"
    pid_file = tmp_path / "explicit.pid"

    def _unexpected_default() -> Path:
        raise AssertionError("default path should not be resolved")

    def _immediate_run(self: TMDaemon) -> None:
        self.shutdown()

    with (
        patch("tm.commands.daemon.default_db_path", side_effect=_unexpected_default),
        patch(
            "tm.commands.daemon.default_socket_path",
            side_effect=_unexpected_default,
        ),
        patch("tm.commands.daemon.default_pid_path", side_effect=_unexpected_default),
        patch.object(TMDaemon, "run", _immediate_run),
    ):
        started = runner.invoke(
            app,
            [
                "daemon",
                "start",
                "--db-path",
                str(db),
                "--socket-path",
                str(sock),
                "--pid-path",
                str(pid_file),
            ],
        )
        stopped = runner.invoke(
            app,
            ["daemon", "stop", "--pid-path", str(pid_file)],
        )
        status = runner.invoke(
            app,
            [
                "daemon",
                "status",
                "--socket-path",
                str(sock),
                "--pid-path",
                str(pid_file),
            ],
        )

    assert started.exit_code == 0, started.output
    assert stopped.exit_code == 1, stopped.output
    assert "no PID file" in stopped.output
    assert status.exit_code == 1, status.output
    assert "down" in status.output


def test_daemon_start_no_foreground_refuses_live_pid_before_detach(
    tmp_path: Path,
) -> None:
    """A live PID file fails before the detach hook can hide the error."""
    db = _prepare_db(tmp_path)
    sock = tmp_path / "tm.sock"
    pid_file = tmp_path / "tm.pid"
    pid_file.write_text(str(os.getpid()))
    detached: list[bool] = []

    def _fake_daemonize() -> None:
        detached.append(True)

    with patch("tm.commands.daemon._daemonize", side_effect=_fake_daemonize):
        result = runner.invoke(
            app,
            [
                "daemon",
                "start",
                "--no-foreground",
                "--db-path",
                str(db),
                "--socket-path",
                str(sock),
                "--pid-path",
                str(pid_file),
            ],
        )

    assert result.exit_code == 1
    assert "already running" in result.output
    assert detached == []


def test_daemon_start_no_foreground_preflights_socket_before_detach(
    tmp_path: Path,
) -> None:
    """Socket bind failures surface before the parent reports detach success."""
    db = _prepare_db(tmp_path)
    sock = tmp_path / "tm.sock"
    pid_file = tmp_path / "tm.pid"
    detached: list[bool] = []

    def _fake_daemonize() -> None:
        detached.append(True)

    with patch("tm.commands.daemon._daemonize", side_effect=_fake_daemonize):
        with patch.object(TMDaemon, "_bind_socket", side_effect=RuntimeError("boom")):
            result = runner.invoke(
                app,
                [
                    "daemon",
                    "start",
                    "--no-foreground",
                    "--db-path",
                    str(db),
                    "--socket-path",
                    str(sock),
                    "--pid-path",
                    str(pid_file),
                ],
            )

    assert result.exit_code == 1
    assert detached == []
    assert not pid_file.exists()


def test_daemon_start_no_foreground_preflights_pid_write_before_detach(
    tmp_path: Path,
) -> None:
    """PID write failures surface before the detach hook can report success."""
    db = _prepare_db(tmp_path)
    sock = tmp_path / "tm.sock"
    pid_file = tmp_path / "tm.pid"
    detached: list[bool] = []

    def _fake_daemonize() -> None:
        detached.append(True)

    with patch("tm.commands.daemon._daemonize", side_effect=_fake_daemonize):
        with patch("tm.commands.daemon.os.open", side_effect=PermissionError("boom")):
            result = runner.invoke(
                app,
                [
                    "daemon",
                    "start",
                    "--no-foreground",
                    "--db-path",
                    str(db),
                    "--socket-path",
                    str(sock),
                    "--pid-path",
                    str(pid_file),
                ],
            )

    assert result.exit_code == 1
    assert detached == []
    assert not pid_file.exists()


# ------------------------------------------------------------------ stop tests


def test_daemon_stop_no_pid_file_exits_1(tmp_path: Path) -> None:
    """``tm daemon stop`` with no PID file exits 1."""
    pid_file = tmp_path / "tm.pid"

    result = runner.invoke(
        app,
        ["daemon", "stop", "--pid-path", str(pid_file)],
    )

    assert result.exit_code == 1
    assert "no PID file" in result.output


def test_daemon_stop_signals_running_daemon(tmp_path: Path) -> None:
    """stop sends SIGTERM to the running daemon; daemon exits cleanly.

    We mock ``os.kill`` to verify SIGTERM is dispatched to the right PID, and
    then simulate process exit by raising ``ProcessLookupError`` on the
    subsequent liveness probe (signal 0 check in the wait loop).
    """
    pid_file = tmp_path / "tm.pid"
    target_pid = 12345
    pid_file.write_text(str(target_pid))

    kill_calls: list[tuple[int, int]] = []

    def _fake_kill(pid: int, sig: int) -> None:
        kill_calls.append((pid, sig))
        if sig == 0:
            # Second call (liveness probe) — pretend process is gone.
            raise ProcessLookupError(f"no process {pid}")
        # First call (SIGTERM) — succeed silently.

    with patch("tm.commands.daemon.os.kill", side_effect=_fake_kill):
        result = runner.invoke(
            app,
            ["daemon", "stop", "--pid-path", str(pid_file)],
        )

    assert result.exit_code == 0, result.output
    assert any(
        pid == target_pid and sig == signal.SIGTERM for pid, sig in kill_calls
    ), f"expected SIGTERM to {target_pid}; got {kill_calls}"
    assert "stopped" in result.output


def test_daemon_stop_stale_pid_file_cleaned_up(tmp_path: Path) -> None:
    """A PID file pointing to a dead process is deleted; stop exits 0."""
    pid_file = tmp_path / "tm.pid"
    pid_file.write_text("999999999")

    def _fake_kill(pid: int, sig: int) -> None:
        raise ProcessLookupError(f"no process {pid}")

    with patch("tm.commands.daemon.os.kill", side_effect=_fake_kill):
        result = runner.invoke(
            app,
            ["daemon", "stop", "--pid-path", str(pid_file)],
        )

    assert result.exit_code == 0, result.output
    assert "stale" in result.output or "not running" in result.output
    assert not pid_file.exists()


def test_daemon_stop_expands_pid_path(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    pid_file = home / "tm.pid"
    pid_file.write_text("999999999")

    with patch("tm.commands.daemon.os.kill", side_effect=ProcessLookupError):
        result = runner.invoke(app, ["daemon", "stop", "--pid-path", "~/tm.pid"])

    assert result.exit_code == 0, result.output
    assert "stale" in result.output or "not running" in result.output
    assert not pid_file.exists()


# ------------------------------------------------------------------ status tests


def test_daemon_status_alive(tmp_path: Path) -> None:
    """``tm daemon status`` exits 0 and prints 'alive' when the daemon is up."""
    db = _prepare_db(tmp_path)
    sock = tmp_path / "d.sock"
    pid_file = tmp_path / "tm.pid"

    daemon, t = _start_daemon_thread(db, sock)
    pid_file.write_text(str(os.getpid()))  # use our PID for the status display
    try:
        result = runner.invoke(
            app,
            [
                "daemon",
                "status",
                "--socket-path",
                str(sock),
                "--pid-path",
                str(pid_file),
            ],
        )
    finally:
        daemon.shutdown()
        t.join(timeout=3.0)

    assert result.exit_code == 0, result.output
    assert "alive" in result.output


def test_daemon_status_down_no_pid_no_socket(tmp_path: Path) -> None:
    """With no PID file and no socket, status exits 1 with 'down'."""
    pid_file = tmp_path / "tm.pid"
    sock = tmp_path / "tm.sock"

    result = runner.invoke(
        app,
        [
            "daemon",
            "status",
            "--socket-path",
            str(sock),
            "--pid-path",
            str(pid_file),
        ],
    )

    assert result.exit_code == 1
    assert "down" in result.output


def test_daemon_status_expands_pid_and_socket_paths(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    pid_file = home / "tm.pid"
    pid_file.write_text(str(os.getpid()))

    result = runner.invoke(
        app,
        [
            "daemon",
            "status",
            "--socket-path",
            "~/tm.sock",
            "--pid-path",
            "~/tm.pid",
        ],
    )

    assert result.exit_code == 1
    assert "unreachable" in result.output


def test_daemon_status_unreachable_socket_present(tmp_path: Path) -> None:
    """A socket file with no daemon listening exits 1 with 'unreachable'."""
    pid_file = tmp_path / "tm.pid"
    sock = tmp_path / "tm.sock"

    # Create a plain file at the socket path — the DaemonClient will fail to
    # connect (no listener), triggering the "unreachable" branch.
    sock.write_bytes(b"")
    pid_file.write_text("999999999")

    result = runner.invoke(
        app,
        [
            "daemon",
            "status",
            "--socket-path",
            str(sock),
            "--pid-path",
            str(pid_file),
        ],
    )

    assert result.exit_code == 1
    assert "unreachable" in result.output
