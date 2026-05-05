"""Tests for VocabularyRepository (T-VOC-01).

Each test uses an isolated in-memory SQLite database with migrations applied
via the Store facade so the full migration chain (0001 → 0004) is exercised.

Tests cover:
- Seeding idempotency for vocabulary and aliases
- list_active / list_all filtering
- archive (status change + idempotency)
- get (present / absent)
- resolve (canonical lookup, alias lookup, case-insensitive, unknown)
- add_alias validation and persistence
- FK enforcement under direct DELETE (alias blocks canonical row deletion)
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

from tm.repositories.vocabulary import (
    STARTER_ALIASES,
    STARTER_VOCABULARY,
    VocabularyEntry,
    VocabularyRepository,
)
from tm.store import Store

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = REPO_ROOT / "migrations"


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    """Per-test isolated SQLite DB with all migrations applied."""
    path = tmp_path / "vocab_test.db"
    store = Store(path, migrations_dir=MIGRATIONS_DIR)
    store.apply_pending_migrations()
    store.close()
    return path


@pytest.fixture()
def repo(db_path: Path) -> VocabularyRepository:
    return VocabularyRepository(db_path)


@pytest.fixture()
def seeded_repo(repo: VocabularyRepository) -> VocabularyRepository:
    """Repository with starter vocabulary (and aliases) already seeded."""
    repo.seed_starter_vocabulary()
    repo.seed_starter_aliases()
    return repo


# ---------------------------------------------------------------------------
# Seeding tests
# ---------------------------------------------------------------------------


def test_seed_inserts_16_starter_activities(repo: VocabularyRepository) -> None:
    count = repo.seed_starter_vocabulary()
    assert count == 16

    entries = repo.list_all()
    names = {e.activity_name for e in entries}
    for expected in STARTER_VOCABULARY:
        assert expected in names, f"Missing starter activity: {expected!r}"


def test_seed_idempotent(repo: VocabularyRepository) -> None:
    first = repo.seed_starter_vocabulary()
    assert first == 16
    second = repo.seed_starter_vocabulary()
    assert second == 0


def test_seed_aliases_inserts_expected_count(repo: VocabularyRepository) -> None:
    repo.seed_starter_vocabulary()
    count = repo.seed_starter_aliases()
    assert count == len(STARTER_ALIASES)
    assert count > 0, "STARTER_ALIASES must contain at least one entry"


def test_seed_aliases_idempotent(repo: VocabularyRepository) -> None:
    repo.seed_starter_vocabulary()
    first = repo.seed_starter_aliases()
    assert first == len(STARTER_ALIASES)
    second = repo.seed_starter_aliases()
    assert second == 0


# ---------------------------------------------------------------------------
# list_active / list_all
# ---------------------------------------------------------------------------


def test_list_active_returns_only_active(seeded_repo: VocabularyRepository) -> None:
    active = seeded_repo.list_active()
    assert all(e.status == "active" for e in active)
    assert len(active) == 16

    # Archive one and verify list_active shrinks
    seeded_repo.archive("sleep")
    active_after = seeded_repo.list_active()
    assert len(active_after) == 15
    assert all(e.activity_name != "sleep" for e in active_after)


def test_list_all_includes_archived(seeded_repo: VocabularyRepository) -> None:
    seeded_repo.archive("rest")
    all_entries = seeded_repo.list_all()
    assert len(all_entries) == 16
    statuses = {e.activity_name: e.status for e in all_entries}
    assert statuses["rest"] == "archived"


# ---------------------------------------------------------------------------
# archive
# ---------------------------------------------------------------------------


def test_archive_changes_status(seeded_repo: VocabularyRepository) -> None:
    seeded_repo.archive("commute")
    entry = seeded_repo.get("commute")
    assert entry is not None
    assert entry.status == "archived"

    active_names = {e.activity_name for e in seeded_repo.list_active()}
    assert "commute" not in active_names


def test_archive_idempotent(seeded_repo: VocabularyRepository) -> None:
    seeded_repo.archive("errand")
    # Second call must not raise
    seeded_repo.archive("errand")
    entry = seeded_repo.get("errand")
    assert entry is not None
    assert entry.status == "archived"


# ---------------------------------------------------------------------------
# get
# ---------------------------------------------------------------------------


def test_get_returns_entry_or_none(seeded_repo: VocabularyRepository) -> None:
    entry = seeded_repo.get("deep_work")
    assert entry is not None
    assert isinstance(entry, VocabularyEntry)
    assert entry.activity_name == "deep_work"
    assert entry.vocab_version == "vocab_v1"
    assert entry.status == "active"

    missing = seeded_repo.get("nonexistent_activity")
    assert missing is None


# ---------------------------------------------------------------------------
# resolve
# ---------------------------------------------------------------------------


def test_resolve_canonical_name_returns_self_lowercased(
    seeded_repo: VocabularyRepository,
) -> None:
    # Mixed-case input should resolve to the stored lowercase name
    result = seeded_repo.resolve("Deep_Work")
    assert result == "deep_work"

    result_lower = seeded_repo.resolve("deep_work")
    assert result_lower == "deep_work"


def test_resolve_alias_returns_canonical(seeded_repo: VocabularyRepository) -> None:
    # 'workout' → 'exercise' is in STARTER_ALIASES
    result = seeded_repo.resolve("workout")
    assert result == "exercise"

    # 'lunch' → 'meal'
    result2 = seeded_repo.resolve("lunch")
    assert result2 == "meal"


def test_resolve_unknown_returns_none(seeded_repo: VocabularyRepository) -> None:
    result = seeded_repo.resolve("totally_unknown_label")
    assert result is None


# ---------------------------------------------------------------------------
# add_alias
# ---------------------------------------------------------------------------


def test_add_alias_raises_on_unknown_canonical(repo: VocabularyRepository) -> None:
    repo.seed_starter_vocabulary()
    with pytest.raises(ValueError, match="not found in vocabulary"):
        repo.add_alias("my_variant", "nonexistent_canonical")


def test_add_alias_persists_and_resolves(repo: VocabularyRepository) -> None:
    repo.seed_starter_vocabulary()
    repo.add_alias("deep_session", "deep_work")
    result = repo.resolve("deep_session")
    assert result == "deep_work"


# ---------------------------------------------------------------------------
# FK: direct DELETE on canonical blocked when aliases exist
# ---------------------------------------------------------------------------


def test_alias_fk_blocks_canonical_archive_with_aliases(
    db_path: Path,
    seeded_repo: VocabularyRepository,
) -> None:
    """Rationale: ``archive()`` only UPDATEs status — it never DELETEs the
    vocabulary row, so FK constraints are never triggered by normal usage.
    This test verifies the underlying FK enforcement works correctly by
    attempting a raw ``DELETE`` on a canonical row that has an alias pointing
    to it.  The ``ON DELETE RESTRICT`` clause on ``aliases.canonical_activity``
    must raise ``sqlite3.IntegrityError``.

    This provides a safety-net: if future code ever tries to hard-delete a
    vocabulary entry while aliases still reference it, SQLite will block it.
    """
    # Add a custom alias so we control the FK relationship explicitly
    seeded_repo.add_alias("gym_session", "exercise")

    # Attempt direct DELETE via raw connection — should fail
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM vocabulary WHERE activity_name = 'exercise'")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# add_canonical (T-VOC-03)
# ---------------------------------------------------------------------------


def test_add_canonical_inserts_new_entry(repo: VocabularyRepository) -> None:
    """add_canonical inserts a new active entry with vocab_v1 version."""
    entry = repo.add_canonical("yoga", "Stretching/relaxation practice")
    assert entry.activity_name == "yoga"
    assert entry.description == "Stretching/relaxation practice"
    assert entry.status == "active"
    assert entry.vocab_version == "vocab_v1"
    assert entry.added_at  # non-empty timestamp

    # Verify it's queryable through normal read methods
    fetched = repo.get("yoga")
    assert fetched is not None
    assert fetched.activity_name == "yoga"


def test_add_canonical_rejects_duplicate(repo: VocabularyRepository) -> None:
    """Adding the same activity_name twice raises ValueError."""
    repo.add_canonical("yoga")
    with pytest.raises(ValueError, match="activity already exists: yoga"):
        repo.add_canonical("yoga")


def test_add_canonical_rejects_uppercase(repo: VocabularyRepository) -> None:
    """activity_name with uppercase letters raises ValueError."""
    with pytest.raises(ValueError, match="lowercase"):
        repo.add_canonical("YOGA")


def test_add_canonical_rejects_empty(repo: VocabularyRepository) -> None:
    """Empty activity_name raises ValueError."""
    with pytest.raises(ValueError, match="must not be empty"):
        repo.add_canonical("")


# ---------------------------------------------------------------------------
# db_path @property (T-VOC-01-polish AC1)
# ---------------------------------------------------------------------------


def test_db_path_property_returns_path(db_path: Path) -> None:
    """VocabularyRepository.db_path returns the path supplied at construction."""
    repo = VocabularyRepository(db_path)
    assert str(repo.db_path) == str(db_path)


def test_db_path_is_readonly(db_path: Path) -> None:
    """db_path is a read-only @property; assignment must raise AttributeError."""
    repo = VocabularyRepository(db_path)
    with pytest.raises(AttributeError):
        repo.db_path = db_path  # type: ignore[misc]


# ---------------------------------------------------------------------------
# add_alias case-normalization (T-VOC-01-polish AC2 / AC3)
# ---------------------------------------------------------------------------


def test_add_alias_lowercases_variant(repo: VocabularyRepository) -> None:
    """Mixed-case free_text_variant is lowercased before storage.

    Regression for case-asymmetry bug: add_alias('GymSession', 'exercise')
    followed by resolve('gymsession') must return 'exercise'.
    """
    repo.seed_starter_vocabulary()
    repo.add_alias("GymSession", "exercise")

    # resolve() lowercases its input, so 'gymsession' must now hit the alias.
    assert repo.resolve("gymsession") == "exercise"
    # resolve() also normalizes mixed-case input directly.
    assert repo.resolve("GymSession") == "exercise"


def test_add_alias_strips_whitespace(repo: VocabularyRepository) -> None:
    """Leading/trailing whitespace is stripped from free_text_variant."""
    repo.seed_starter_vocabulary()
    repo.add_alias("  yoga  ", "exercise")
    assert repo.resolve("yoga") == "exercise"


def test_add_alias_rejects_empty_after_strip(repo: VocabularyRepository) -> None:
    """Whitespace-only free_text_variant raises ValueError after strip."""
    repo.seed_starter_vocabulary()
    with pytest.raises(ValueError, match="non-empty after strip"):
        repo.add_alias("   ", "exercise")


# ---------------------------------------------------------------------------
# ISO-T-Z timestamp format (T-VOC-01-polish AC4)
# ---------------------------------------------------------------------------

_ISO_TZ_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def test_seed_starter_vocabulary_uses_iso_t_z_format(db_path: Path) -> None:
    """seed_starter_vocabulary() persists added_at in ISO-T-Z format."""
    repo = VocabularyRepository(db_path)
    repo.seed_starter_vocabulary()

    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute("SELECT added_at FROM vocabulary").fetchall()
    finally:
        conn.close()

    assert rows, "Expected at least one vocabulary row"
    for (added_at,) in rows:
        assert _ISO_TZ_RE.match(added_at), (
            f"added_at {added_at!r} does not match ISO-T-Z pattern"
        )


def test_add_canonical_uses_iso_t_z_format(db_path: Path) -> None:
    """add_canonical() persists added_at in ISO-T-Z format."""
    repo = VocabularyRepository(db_path)
    entry = repo.add_canonical("yoga")

    assert _ISO_TZ_RE.match(entry.added_at), (
        f"added_at {entry.added_at!r} does not match ISO-T-Z pattern"
    )

    # Cross-check via raw SQL.
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT added_at FROM vocabulary WHERE activity_name = 'yoga'"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert _ISO_TZ_RE.match(row[0]), f"Raw added_at {row[0]!r} does not match ISO-T-Z"


def test_add_alias_uses_iso_t_z_format(db_path: Path) -> None:
    """add_alias() persists created_at in ISO-T-Z format."""
    repo = VocabularyRepository(db_path)
    repo.seed_starter_vocabulary()
    repo.add_alias("gym", "exercise")

    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT created_at FROM aliases WHERE free_text_variant = 'gym'"
        ).fetchone()
    finally:
        conn.close()

    assert row is not None, "Alias row not found"
    assert _ISO_TZ_RE.match(row[0]), (
        f"created_at {row[0]!r} does not match ISO-T-Z pattern"
    )
