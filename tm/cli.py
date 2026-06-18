from __future__ import annotations

from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from typing import Annotated

import typer

from tm import __version__
from tm._paths import default_db_path
from tm.commands.bottlenecks import bottlenecks_app
from tm.commands.capture import capture_app
from tm.commands.daemon import daemon_app
from tm.commands.dashboard import dashboard_app
from tm.commands.debrief import debrief_app
from tm.commands.discover import discover_app
from tm.commands.encryption import encryption_app
from tm.commands.export import backup_app, export_app
from tm.commands.goal import goal_app
from tm.commands.privacy import privacy_app
from tm.commands.reextract import reextract_app
from tm.commands.suggest import suggest_app
from tm.commands.variants import variants_app
from tm.commands.vocab import vocab_app
from tm.commands.web import web_app
from tm.repositories.vocabulary import VocabularyRepository
from tm.stores.sqlite_store import SQLiteStore

app = typer.Typer(help="tm — behavioral time manager CLI.")
app.add_typer(backup_app, name="backup")
app.add_typer(bottlenecks_app, name="bottlenecks")
app.add_typer(capture_app, name="capture")
app.add_typer(daemon_app, name="daemon")
app.add_typer(dashboard_app, name="dashboard")
app.add_typer(debrief_app, name="debrief")
app.add_typer(discover_app, name="discover")
app.add_typer(encryption_app, name="encryption")
app.add_typer(export_app, name="export")
app.add_typer(goal_app, name="goal")
app.add_typer(privacy_app, name="privacy")
app.add_typer(reextract_app, name="reextract")
app.add_typer(suggest_app, name="suggest")
app.add_typer(variants_app, name="variants")
app.add_typer(vocab_app, name="vocab")
app.add_typer(web_app, name="web")

_DbPathOption = Annotated[
    Path | None,
    typer.Option(
        "--db-path",
        envvar="TM_DB",
        help="Path to the tm SQLite database.",
        show_default=False,
    ),
]


def version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        "-v",
        callback=version_callback,
        is_eager=True,
        help="Show version and exit.",
    ),
) -> None:
    pass


@app.command()
def init(db_path: _DbPathOption = None) -> None:
    """Initialise a new tm workspace."""
    db_path = db_path or default_db_path()

    store = SQLiteStore(db_path)
    try:
        with redirect_stderr(StringIO()):
            applied_migrations = store.apply_pending_migrations()
    finally:
        store.close()

    vocab_repo = VocabularyRepository(db_path)
    seeded_activities = vocab_repo.seed_starter_vocabulary()
    seeded_aliases = vocab_repo.seed_starter_aliases()

    typer.echo(f"tm init: db={db_path}")
    typer.echo(f"  applied {len(applied_migrations)} migrations")
    typer.echo(f"  seeded {seeded_activities} starter activities")
    typer.echo(f"  seeded {seeded_aliases} starter aliases")
