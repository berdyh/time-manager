"""Tests that mypy correctly enforces the StoreReader Protocol.

Strategy:
1. Run mypy on probe.py as-is — the ``# type: ignore[attr-defined]``
   comments suppress errors so the run should be clean (exit 0).
2. Copy probe.py to a tmp file with those comments stripped, then run mypy
   on the copy — the run MUST fail (exit != 0) and the output must mention
   ``attr-defined`` twice (once per write-method call).

This gives us a mechanical CI guard: if anyone ever widens StoreReader to
include write methods, the type-ignore comments in probe.py become unused and
``--warn-unused-ignores`` will fire, breaking the suite.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

PROBE_PATH = Path(__file__).parent / "probe.py"
# Repo root: two levels up from tests/types/
REPO_ROOT = Path(__file__).resolve().parents[2]

# Skip the whole module if mypy isn't importable in the running interpreter.
# We invoke mypy as ``sys.executable -m mypy`` below, so a PATH-based check
# (e.g. shutil.which("mypy")) over-skips: sandboxed CI may have mypy
# importable in the venv without exposing the entry-point on PATH.
try:
    import mypy  # noqa: F401 — import probe only
except ImportError:
    pytest.skip("mypy not importable in this interpreter", allow_module_level=True)


def _run_mypy(*extra_args: str, source: Path) -> subprocess.CompletedProcess[str]:
    """Run mypy on *source* with optional extra arguments.

    ``--explicit-package-bases`` is required so that the ``tests/types/``
    directory does not shadow the stdlib ``types`` module.  We always run with
    ``cwd=REPO_ROOT`` and set ``MYPYPATH`` so that ``import tm.store`` resolves
    correctly even when *source* lives outside the project tree (e.g. in
    ``tmp_path``).
    """
    import os

    env = os.environ.copy()
    env["MYPYPATH"] = str(REPO_ROOT)

    cmd = [
        sys.executable,
        "-m",
        "mypy",
        "--no-incremental",
        "--explicit-package-bases",
        *extra_args,
        str(source),
    ]
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(REPO_ROOT),
        env=env,
    )


def test_probe_clean_with_ignores() -> None:
    """probe.py as shipped (type: ignore comments intact) must be mypy-clean."""
    result = _run_mypy(source=PROBE_PATH)
    assert result.returncode == 0, (
        f"mypy reported errors on probe.py (ignores should suppress them):\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


def test_probe_clean_with_warn_unused_ignores() -> None:
    """With --warn-unused-ignores, probe.py must still be clean.

    If StoreReader is ever widened to include write methods the type:ignore
    comments become unused and this test will fail — that is the guard.
    """
    result = _run_mypy("--warn-unused-ignores", source=PROBE_PATH)
    assert result.returncode == 0, (
        f"mypy --warn-unused-ignores reported errors; did StoreReader gain write "
        f"methods?\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )


def test_attr_defined_errors_when_ignores_stripped(tmp_path: Path) -> None:
    """Strip type:ignore comments → mypy must report attr-defined errors.

    This is the key assertion: it proves the Protocol is actively enforcing
    the boundary between read-only callers and write methods.
    """
    original = PROBE_PATH.read_text()
    stripped = re.sub(r"\s*#\s*type:\s*ignore\[attr-defined\]", "", original)

    stripped_path = tmp_path / "probe_stripped.py"
    stripped_path.write_text(stripped)

    result = _run_mypy(source=stripped_path)

    assert result.returncode != 0, (
        "mypy should have failed on the stripped probe (attr-defined errors expected) "
        "but it exited 0. Is StoreReader too wide?"
    )

    combined = result.stdout + result.stderr
    occurrences = combined.count("attr-defined")
    assert occurrences >= 2, (
        f"Expected at least 2 'attr-defined' errors (one per write-method call), "
        f"got {occurrences}.\nOutput:\n{combined}"
    )
