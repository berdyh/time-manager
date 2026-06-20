"""tm web CLI: run the local cockpit UI."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from tm._paths import default_db_path, default_socket_path

web_app = typer.Typer(help="Web — run the local cockpit UI.")


@web_app.callback(invoke_without_command=True)
def web(
    db_path: Annotated[
        Path | None,
        typer.Option("--db-path", envvar="TM_DB", help="Path to the SQLite database."),
    ] = None,
    socket_path: Annotated[
        Path | None,
        typer.Option(
            "--socket-path", envvar="TM_SOCKET", help="Path to the Unix daemon socket."
        ),
    ] = None,
    host: Annotated[str, typer.Option("--host", help="Bind host.")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", help="Bind port.")] = 8765,
    static_dir: Annotated[
        Path | None,
        typer.Option("--static-dir", help="Built frontend directory."),
    ] = None,
) -> None:
    """Run the local web UI server."""

    try:
        import uvicorn
    except ImportError as exc:
        typer.echo("error: install tm with the 'web' extra to run tm web", err=True)
        raise typer.Exit(1) from exc

    from tm.web.app import create_app

    app = create_app(
        db_path=db_path or default_db_path(),
        socket_path=socket_path or default_socket_path(),
        static_dir=static_dir,
    )
    uvicorn.run(app, host=host, port=port, log_level="info")
