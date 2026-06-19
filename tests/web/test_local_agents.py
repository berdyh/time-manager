"""Tests for local agent configuration path behavior."""

from __future__ import annotations

import importlib
import sys

import pytest

import tm._paths as tm_paths


def test_local_agents_import_does_not_create_default_data_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_module = sys.modules.pop("tm.local_agents", None)

    def fail_default_data_dir() -> None:
        raise AssertionError("default_data_dir should be lazy")

    monkeypatch.setattr(tm_paths, "default_data_dir", fail_default_data_dir)
    try:
        module = importlib.import_module("tm.local_agents")
        assert callable(module.default_agent_config_path)
    finally:
        sys.modules.pop("tm.local_agents", None)
        if original_module is not None:
            sys.modules["tm.local_agents"] = original_module
