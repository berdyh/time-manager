"""tm daemon CLI: start, stop, status.

v1 supports foreground mode only (``--foreground``).  Real daemonization
(double-fork, setsid, stdio redirect) is deferred to a future task; production
deployments should use a process supervisor (systemd, launchd, etc.) that
invokes ``tm daemon start --foreground``.

Signal handling
---------------
``start`` installs SIGTERM and SIGINT handlers that call
:meth:`~tm.daemon.TMDaemon.shutdown`.  Python only delivers signals to the
main thread; since ``start`` runs ``daemon.run()`` on the calling (main)
thread this is fine in production.  Tests that need to exercise signal
delivery use a subprocess or mock ``os.kill`` / ``daemon.shutdown`` directly
to avoid the main-thread restriction.
"""

from __future__ import annotations

import os
import signal
import time
from pathlib import Path
from typing import Annotated

import typer

from tm._paths import default_db_path, default_pid_path, default_socket_path
from tm.daemon import DaemonClient, TMDaemon

daemon_app = typer.Typer(help="Daemon — start, stop, status.")


# ------------------------------------------------------------------ start


@daemon_app.command("start")
def start(
    db_path: Annotated[
        Path | None,
        typer.Option("--db-path", envvar="TM_DB", help="Path to the SQLite database."),
    ] = None,
    socket_path: Annotated[
        Path | None,
        typer.Option(
            "--socket-path", envvar="TM_SOCKET", help="Path to the Unix domain socket."
        ),
    ] = None,
    pid_path: Annotated[
        Path | None,
        typer.Option("--pid-path", envvar="TM_PID", help="Path to the PID file."),
    ] = None,
    foreground: Annotated[
        bool,
        typer.Option(
            "--foreground/--no-foreground",
            help="Run in foreground (blocking). Only --foreground is supported in v1.",
        ),
    ] = True,
) -> None:
    """Start the daemon.  Blocks until SIGTERM/SIGINT.

    Only foreground mode is supported in v1 (``--foreground``).  Pass
    ``--no-foreground`` to get an explicit error rather than silent failure.
    """
    if not foreground:
        typer.echo("error: only --foreground is supported in v1", err=True)
        raise typer.Exit(2)

    resolved_db = db_path or default_db_path()
    resolved_socket = socket_path or default_socket_path()
    resolved_pid = pid_path or default_pid_path()

    # ---- stale / live PID check ------------------------------------------
    if resolved_pid.exists():
        try:
            old_pid = int(resolved_pid.read_text().strip())
            try:
                os.kill(old_pid, 0)  # signal 0: check existence only
                typer.echo(
                    f"error: daemon already running (PID {old_pid},"
                    f" file {resolved_pid})",
                    err=True,
                )
                raise typer.Exit(1)
            except ProcessLookupError:
                # Stale PID file — the old process is gone.
                resolved_pid.unlink(missing_ok=True)
        except (ValueError, OSError):
            resolved_pid.unlink(missing_ok=True)

    # ---- build daemon + write PID ----------------------------------------
    daemon = TMDaemon(db_path=resolved_db, socket_path=resolved_socket)
    resolved_pid.write_text(str(os.getpid()))

    # ---- install signal handlers -----------------------------------------
    # Handlers call shutdown(); run()'s finally-block handles cleanup.
    def _handle_signal(signum: int, frame: object) -> None:  # noqa: ARG001
        daemon.shutdown()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    # ---- block until shutdown --------------------------------------------
    try:
        daemon.run()
    finally:
        resolved_pid.unlink(missing_ok=True)


# ------------------------------------------------------------------ stop


@daemon_app.command("stop")
def stop(
    pid_path: Annotated[
        Path | None,
        typer.Option("--pid-path", envvar="TM_PID", help="Path to the PID file."),
    ] = None,
    timeout: Annotated[
        float,
        typer.Option("--timeout", help="Seconds to wait for daemon to exit."),
    ] = 5.0,
) -> None:
    """Stop the daemon by reading the PID file and sending SIGTERM."""
    resolved_pid = pid_path or default_pid_path()

    if not resolved_pid.exists():
        typer.echo(f"error: no PID file at {resolved_pid}", err=True)
        raise typer.Exit(1)

    try:
        pid = int(resolved_pid.read_text().strip())
    except (ValueError, OSError) as exc:
        typer.echo(f"error: malformed PID file: {exc}", err=True)
        raise typer.Exit(1) from None

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        typer.echo(f"warning: process {pid} not running; cleaning up stale PID file")
        resolved_pid.unlink(missing_ok=True)
        raise typer.Exit(0) from None
    except OSError as exc:
        typer.echo(f"error: failed to signal {pid}: {exc}", err=True)
        raise typer.Exit(1) from None

    # Wait for the process to exit.
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
            time.sleep(0.05)
        except ProcessLookupError:
            typer.echo(f"daemon stopped (PID {pid})")
            return

    typer.echo(f"warning: daemon (PID {pid}) did not exit within {timeout}s", err=True)
    raise typer.Exit(1)


# ------------------------------------------------------------------ status


@daemon_app.command("status")
def status(
    socket_path: Annotated[
        Path | None,
        typer.Option(
            "--socket-path", envvar="TM_SOCKET", help="Path to the Unix domain socket."
        ),
    ] = None,
    pid_path: Annotated[
        Path | None,
        typer.Option("--pid-path", envvar="TM_PID", help="Path to the PID file."),
    ] = None,
) -> None:
    """Report daemon status: alive (responds to ping) or down."""
    resolved_socket = socket_path or default_socket_path()
    resolved_pid = pid_path or default_pid_path()

    pid_present = resolved_pid.exists()
    socket_present = resolved_socket.exists()

    if not pid_present and not socket_present:
        typer.echo("daemon: down (no PID, no socket)")
        raise typer.Exit(1)

    # Try to ping — this is the authoritative liveness check.
    try:
        client = DaemonClient(resolved_socket, timeout=2.0)
        client.call("ping")
    except Exception as exc:
        typer.echo(f"daemon: unreachable ({exc}); cleaning up if stale", err=True)
        raise typer.Exit(1) from None

    # Ping succeeded — daemon is alive.
    if pid_present:
        try:
            pid = int(resolved_pid.read_text().strip())
            typer.echo(f"daemon: alive (PID {pid}, socket {resolved_socket})")
            return
        except (ValueError, OSError):
            pass
    typer.echo(f"daemon: alive (socket {resolved_socket})")
