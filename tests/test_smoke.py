"""CLI smoke tests — fast sanity checks for the tm entry point.

Uses typer.testing.CliRunner so no subprocess overhead is needed.
"""

from __future__ import annotations

import re

from typer.testing import CliRunner

from tm.cli import app

runner = CliRunner()


def test_version() -> None:
    """``tm --version`` exits 0 and prints a semver string."""
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0, (
        f"unexpected exit code: {result.exit_code}\n{result.output}"
    )
    assert re.match(r"^\d+\.\d+\.\d+\s*$", result.stdout), (
        f"output does not look like semver: {result.stdout!r}"
    )


def test_init_stub() -> None:
    """``tm init`` exits 0 and prints the stub message."""
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0, (
        f"unexpected exit code: {result.exit_code}\n{result.output}"
    )
    assert "tm init not yet implemented" in result.stdout
