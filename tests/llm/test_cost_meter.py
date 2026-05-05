"""Tests for tm.llm.cost_meter.CostMeter.

Covers the budget gate, ledger writes, pricing-table lookups, soft-alarm
behaviour, and env-var overrides. No network or real LLM calls.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tm.llm.cost_meter import (
    DEFAULT_MONTHLY_CAP_USD,
    PRICING_USD_PER_1M,
    CostMeter,
    estimate_cost_usd,
)
from tm.llm.errors import CostCapExceeded
from tm.store import Store

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = REPO_ROOT / "migrations"


@pytest.fixture()
def db_path_with_ledger(tmp_path: Path) -> Path:
    """Return a db_path with all migrations applied (incl. 0002_cost_ledger)."""
    db = tmp_path / "tm.db"
    s = Store(db, migrations_dir=MIGRATIONS_DIR)
    s.apply_pending_migrations()
    s.close()
    return db


# --------------------------------------------------------------- pricing


def test_pricing_table_covers_required_models() -> None:
    """All three required Anthropic model ids must be priced."""
    assert "claude-opus-4-7" in PRICING_USD_PER_1M
    assert "claude-sonnet-4-6" in PRICING_USD_PER_1M
    assert "claude-haiku-4-5-20251001" in PRICING_USD_PER_1M
    for model, (in_p, out_p) in PRICING_USD_PER_1M.items():
        assert in_p > 0, f"{model}: input price should be positive"
        assert out_p > 0, f"{model}: output price should be positive"


def test_estimate_cost_known_model() -> None:
    """Estimate equals (in/1M)*in_price + (out/1M)*out_price."""
    cost = estimate_cost_usd(
        model="claude-sonnet-4-6",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
    )
    in_p, out_p = PRICING_USD_PER_1M["claude-sonnet-4-6"]
    assert cost == pytest.approx(in_p + out_p)


def test_estimate_cost_unknown_model_warns_and_returns_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Use a unique name so prior test runs in the same process don't suppress.
    cost = estimate_cost_usd(
        model="totally-bogus-model-zzz",
        input_tokens=10_000,
        output_tokens=10_000,
    )
    assert cost == 0.0
    captured = capsys.readouterr()
    assert "totally-bogus-model-zzz" in captured.err
    assert "unknown model" in captured.err


# --------------------------------------------------------- monthly_total


def test_monthly_total_starts_zero(db_path_with_ledger: Path) -> None:
    meter = CostMeter(db_path_with_ledger)
    assert meter.monthly_total() == 0.0


def test_record_increases_monthly_total(db_path_with_ledger: Path) -> None:
    meter = CostMeter(db_path_with_ledger)
    cost = meter.record(
        model="claude-sonnet-4-6",
        input_tokens=10_000,
        output_tokens=5_000,
        request_kind="chat",
    )
    assert cost > 0.0
    assert meter.monthly_total() == pytest.approx(cost)

    cost2 = meter.record(
        model="claude-haiku-4-5-20251001",
        input_tokens=1_000,
        output_tokens=500,
        request_kind="extract",
    )
    assert meter.monthly_total() == pytest.approx(cost + cost2)


def test_record_inserts_row_with_expected_columns(
    db_path_with_ledger: Path,
) -> None:
    meter = CostMeter(db_path_with_ledger)
    meter.record(
        model="claude-opus-4-7",
        input_tokens=2_000,
        output_tokens=1_000,
        request_kind="tool_call",
    )
    conn = sqlite3.connect(db_path_with_ledger)
    try:
        cur = conn.execute(
            "SELECT model, input_tokens, output_tokens, request_kind, "
            "est_cost_usd, ts FROM cost_ledger"
        )
        rows = cur.fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    model, in_t, out_t, kind, cost, ts = rows[0]
    assert model == "claude-opus-4-7"
    assert in_t == 2_000
    assert out_t == 1_000
    assert kind == "tool_call"
    assert cost > 0.0
    assert isinstance(ts, str) and ts.endswith("Z")


def test_record_rejects_unknown_request_kind(db_path_with_ledger: Path) -> None:
    meter = CostMeter(db_path_with_ledger)
    with pytest.raises(ValueError):
        meter.record(
            model="claude-sonnet-4-6",
            input_tokens=10,
            output_tokens=10,
            request_kind="ohai",
        )


def test_record_rejects_negative_tokens(db_path_with_ledger: Path) -> None:
    meter = CostMeter(db_path_with_ledger)
    with pytest.raises(ValueError):
        meter.record(
            model="claude-sonnet-4-6",
            input_tokens=-1,
            output_tokens=10,
            request_kind="chat",
        )


# ----------------------------------------------------------- check_budget


def test_check_budget_passes_when_under_cap(db_path_with_ledger: Path) -> None:
    meter = CostMeter(db_path_with_ledger, monthly_cap_usd=10.0)
    # Total starts 0; estimate well below cap.
    meter.check_budget(0.01)


def test_check_budget_raises_when_over_cap(db_path_with_ledger: Path) -> None:
    meter = CostMeter(db_path_with_ledger, monthly_cap_usd=1.0)
    # Push monthly_total close to the cap.
    meter.record(
        model="claude-opus-4-7",
        input_tokens=60_000,
        output_tokens=0,
        request_kind="chat",
    )
    # 60k input on opus = (60_000 / 1_000_000) * 15 = $0.90
    assert meter.monthly_total() == pytest.approx(0.90)
    # An estimate of $0.20 would push us over $1.00.
    with pytest.raises(CostCapExceeded) as ei:
        meter.check_budget(0.20)
    err = ei.value
    assert err.cap_usd == pytest.approx(1.0)
    assert err.estimate_usd == pytest.approx(0.20)
    assert err.monthly_total_usd == pytest.approx(0.90)


def test_check_budget_record_inserts_even_when_over_cap(
    db_path_with_ledger: Path,
) -> None:
    """record() always inserts; pre-check is the caller's responsibility."""
    meter = CostMeter(db_path_with_ledger, monthly_cap_usd=0.001)
    cost = meter.record(
        model="claude-opus-4-7",
        input_tokens=10_000,
        output_tokens=10_000,
        request_kind="chat",
    )
    assert cost > 0.001
    assert meter.monthly_total() == pytest.approx(cost)


# ----------------------------------------------------------- env-var cap


def test_default_cap_is_twenty(
    db_path_with_ledger: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TM_LLM_MONTHLY_CAP_USD", raising=False)
    meter = CostMeter(db_path_with_ledger)
    assert meter.monthly_cap_usd == DEFAULT_MONTHLY_CAP_USD == 20.0


def test_env_override_for_cap(
    db_path_with_ledger: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TM_LLM_MONTHLY_CAP_USD", "5.50")
    meter = CostMeter(db_path_with_ledger)
    assert meter.monthly_cap_usd == pytest.approx(5.50)


def test_explicit_cap_overrides_env(
    db_path_with_ledger: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TM_LLM_MONTHLY_CAP_USD", "5.50")
    meter = CostMeter(db_path_with_ledger, monthly_cap_usd=99.0)
    assert meter.monthly_cap_usd == pytest.approx(99.0)


def test_invalid_env_falls_back_to_default(
    db_path_with_ledger: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("TM_LLM_MONTHLY_CAP_USD", "not-a-number")
    meter = CostMeter(db_path_with_ledger)
    assert meter.monthly_cap_usd == DEFAULT_MONTHLY_CAP_USD
    err = capsys.readouterr().err
    assert "TM_LLM_MONTHLY_CAP_USD" in err


# ----------------------------------------------------------- soft alarm


def test_soft_alarm_fires_at_70_percent(
    db_path_with_ledger: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """First record() that pushes total >= 70% of cap emits a stderr warning."""
    meter = CostMeter(db_path_with_ledger, monthly_cap_usd=1.0)
    # Record a small call first — well below 70% of $1.00.
    meter.record(
        model="claude-haiku-4-5-20251001",
        input_tokens=1_000,
        output_tokens=0,
        request_kind="chat",
    )
    out_first = capsys.readouterr().err
    assert "70%" not in out_first

    # Now push us across 70%: opus at 50_000 input = $0.75 -> >= $0.70.
    meter.record(
        model="claude-opus-4-7",
        input_tokens=50_000,
        output_tokens=0,
        request_kind="chat",
    )
    out_after = capsys.readouterr().err
    assert "70%" in out_after
    assert "monthly spend" in out_after


def test_soft_alarm_fires_only_once_per_process(
    db_path_with_ledger: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    meter = CostMeter(db_path_with_ledger, monthly_cap_usd=1.0)
    meter.record(
        model="claude-opus-4-7",
        input_tokens=50_000,
        output_tokens=0,
        request_kind="chat",
    )
    first = capsys.readouterr().err
    assert "70%" in first
    # Second crossing: alarm should NOT fire again.
    meter.record(
        model="claude-opus-4-7",
        input_tokens=10_000,
        output_tokens=0,
        request_kind="chat",
    )
    second = capsys.readouterr().err
    assert "70%" not in second
