"""CLI smoke tests for ``tm web``."""

from __future__ import annotations

from typer.testing import CliRunner

from tm.cli import app

runner = CliRunner()


def test_web_help_lists_options() -> None:
    result = runner.invoke(app, ["web", "--help"])

    assert result.exit_code == 0, result.output
    assert "--host" in result.output
    assert "--port" in result.output
