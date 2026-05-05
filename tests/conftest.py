"""Shared pytest fixtures.

Minimal in T-FND-02: just a tmp-path SQLite Store fixture. T-FND-05 will
extend this file additively.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from hypothesis import HealthCheck, settings

from tm.store import Store

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MIGRATIONS_DIR = REPO_ROOT / "migrations"


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    """Filesystem path for an isolated per-test SQLite database."""
    return tmp_path / "tm.db"


@pytest.fixture()
def store(db_path: Path) -> Store:
    """Open a Store on the per-test database with migrations applied."""
    s = Store(db_path, migrations_dir=DEFAULT_MIGRATIONS_DIR)
    s.apply_pending_migrations()
    return s


# ---------------------------------------------------------------------------
# Hypothesis profiles — registered by T-FND-05
# ---------------------------------------------------------------------------
# Register two named profiles:
#   dev  — fast iteration during local development (max_examples=50)
#   ci   — thorough coverage for CI pipelines (max_examples=200)
#
# The active profile defaults to ``dev`` unless the environment variable
# ``TM_TEST_PROFILE=ci`` is set.  Property tests can also activate a
# profile explicitly via ``@settings(max_examples=...)`` or by calling
# ``settings.load_profile("ci")`` at the top of their module.

settings.register_profile(
    "dev",
    max_examples=50,
    suppress_health_check=[HealthCheck.too_slow],
)

settings.register_profile(
    "ci",
    max_examples=200,
    suppress_health_check=[HealthCheck.too_slow],
)

_active_profile = os.environ.get("TM_TEST_PROFILE", "dev")
settings.load_profile(_active_profile)
