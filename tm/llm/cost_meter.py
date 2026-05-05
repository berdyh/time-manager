"""SQLite-backed cost ledger + pre-call budget gate for LLM API calls.

Design intent (per locked v1 design):

* Every LLM call records a row in ``cost_ledger`` (model, ts, tokens,
  estimated USD cost, request kind).
* Before issuing a call, the caller invokes :meth:`CostMeter.check_budget`
  with an estimated USD cost; if ``monthly_total + estimate > cap`` the
  meter raises :class:`tm.llm.errors.CostCapExceeded`.
* A soft alarm fires once per process run when monthly spend crosses 70%
  of the cap (in-memory flag — acceptable for v1; resets on restart).
* Pricing is hardcoded for the three Anthropic models we plan to use; an
  ``unknown_model`` fallback returns $0.0 and warns once on stderr.

The cost meter manages its own SQLite connection rather than going through
:class:`tm.store.Store`, per T-FND-04 task constraints. It opens a fresh
connection per operation pointed at the same db_path. The ledger schema is
applied by migration ``0002_cost_ledger.sql`` via the T-FND-02 migration
runner; the meter does NOT run migrations itself — callers ensure
:meth:`tm.store.Store.apply_pending_migrations` has been invoked before
constructing the meter.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

from tm.llm.errors import CostCapExceeded

__all__ = [
    "DEFAULT_MONTHLY_CAP_USD",
    "PRICING_USD_PER_1M",
    "REQUEST_KINDS",
    "CostMeter",
    "PriceRow",
]


# ---------------------------------------------------------------------- types

# Per-1M-token pricing (input, output) in USD. Hardcoded for v1; if Anthropic
# updates its rate card, bump this dict in a follow-up. The keys must match
# what we send as ``model`` in the Anthropic SDK call.
PriceRow = tuple[float, float]

PRICING_USD_PER_1M: dict[str, PriceRow] = {
    # Frontier reasoning model.
    "claude-opus-4-7": (15.00, 75.00),
    # Mid-tier; default workhorse for most flows.
    "claude-sonnet-4-6": (3.00, 15.00),
    # Cheap, fast — used for cheap classification / extraction.
    "claude-haiku-4-5-20251001": (1.00, 5.00),
}

# Cap defaults / env override.
DEFAULT_MONTHLY_CAP_USD = 20.0
_ENV_CAP = "TM_LLM_MONTHLY_CAP_USD"

# Soft-alarm trip point as a fraction of the cap.
_SOFT_ALARM_FRACTION = 0.70

REQUEST_KINDS: frozenset[str] = frozenset({"chat", "extract", "tool_call"})


# ----------------------------------------------------- module-level state set

# Track which unknown model ids we've already warned about so we don't spam
# stderr on every call. This is process-local; cheap and correct for v1.
_UNKNOWN_MODEL_WARNED: set[str] = set()


# --------------------------------------------------------------- helpers


def _utcnow_iso() -> str:
    """Return current UTC time as an ISO-8601 string with ``Z`` suffix."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _month_floor_iso(now: datetime | None = None) -> str:
    """Return the ISO timestamp for the first instant of the current UTC month.

    Used as a half-open lower bound when summing the month-to-date total.
    """
    n = now or datetime.now(UTC)
    floor = n.replace(day=1, hour=0, minute=0, second=0, microsecond=0, tzinfo=UTC)
    return floor.strftime("%Y-%m-%dT%H:%M:%SZ")


def estimate_cost_usd(*, model: str, input_tokens: int, output_tokens: int) -> float:
    """Return the USD cost for a call given token counts.

    Unknown models cost $0.0 and warn once per process to stderr.
    """
    pricing = PRICING_USD_PER_1M.get(model)
    if pricing is None:
        if model not in _UNKNOWN_MODEL_WARNED:
            _UNKNOWN_MODEL_WARNED.add(model)
            print(
                f"warning: unknown model {model!r}; treating cost as $0.00. "
                "Update PRICING_USD_PER_1M in tm/llm/cost_meter.py.",
                file=sys.stderr,
            )
        return 0.0
    in_per_1m, out_per_1m = pricing
    return (input_tokens / 1_000_000.0) * in_per_1m + (
        output_tokens / 1_000_000.0
    ) * out_per_1m


# ------------------------------------------------------------ class


class CostMeter:
    """SQLite-backed cost ledger and budget gate.

    Construct with the same ``db_path`` used by :class:`tm.store.Store`. The
    ``cost_ledger`` table must already exist (migration ``0002_cost_ledger``
    must have been applied by the migration runner).

    The meter uses a fresh ``sqlite3`` connection per operation. This keeps
    contention with the daemon's long-lived Store connection minimal, since
    the ledger writes are tiny and bounded.
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        monthly_cap_usd: float | None = None,
    ) -> None:
        self._db_path = str(db_path)
        if monthly_cap_usd is not None:
            self._cap = float(monthly_cap_usd)
        else:
            self._cap = self._cap_from_env()
        # In-memory soft-alarm flag: trips once per process run when monthly
        # total crosses _SOFT_ALARM_FRACTION of the cap.
        self._soft_alarm_fired = False

    # -------------------------------------------------------------- accessors

    @property
    def monthly_cap_usd(self) -> float:
        return self._cap

    @staticmethod
    def _cap_from_env() -> float:
        raw = os.environ.get(_ENV_CAP)
        if raw is None or raw.strip() == "":
            return DEFAULT_MONTHLY_CAP_USD
        try:
            return float(raw)
        except ValueError:
            print(
                f"warning: {_ENV_CAP}={raw!r} is not a valid float; "
                f"falling back to default ${DEFAULT_MONTHLY_CAP_USD:.2f}.",
                file=sys.stderr,
            )
            return DEFAULT_MONTHLY_CAP_USD

    # -------------------------------------------------------------- connection

    def _connect(self) -> sqlite3.Connection:
        """Open a short-lived connection. Foreign keys on by default."""
        conn = sqlite3.connect(self._db_path, timeout=5.0)
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    # -------------------------------------------------------------- public API

    def monthly_total(self) -> float:
        """Sum ``est_cost_usd`` for the current UTC calendar month."""
        floor = _month_floor_iso()
        conn = self._connect()
        try:
            cur = conn.execute(
                "SELECT COALESCE(SUM(est_cost_usd), 0.0) FROM cost_ledger "
                "WHERE ts >= ?",
                (floor,),
            )
            row = cur.fetchone()
            cur.close()
            return float(row[0]) if row is not None else 0.0
        finally:
            conn.close()

    def check_budget(self, estimated_cost_usd: float) -> None:
        """Pre-call gate: raise :class:`CostCapExceeded` if the estimate would
        push the running monthly total past the configured cap.

        Callers should compute ``estimated_cost_usd`` themselves (typically by
        feeding ``max_tokens`` into :func:`estimate_cost_usd`).
        """
        total = self.monthly_total()
        if total + float(estimated_cost_usd) > self._cap:
            raise CostCapExceeded(
                monthly_total_usd=total,
                estimate_usd=float(estimated_cost_usd),
                cap_usd=self._cap,
            )

    def record(
        self,
        *,
        model: str,
        input_tokens: int,
        output_tokens: int,
        request_kind: str,
    ) -> float:
        """Insert a ledger row and return its computed ``est_cost_usd``.

        Always records — the pre-call gate is :meth:`check_budget`. Callers
        that want budget enforcement must call :meth:`check_budget` first.
        Soft-alarm: if the running monthly total (post-insert) is at or above
        70% of the cap and the alarm hasn't fired this process run, emit a
        single stderr warning.
        """
        if request_kind not in REQUEST_KINDS:
            raise ValueError(
                f"record: unknown request_kind {request_kind!r}; "
                f"expected one of {sorted(REQUEST_KINDS)}"
            )
        if input_tokens < 0 or output_tokens < 0:
            raise ValueError("record: token counts must be non-negative")

        cost = estimate_cost_usd(
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        ts = _utcnow_iso()

        conn = self._connect()
        try:
            with conn:  # auto-commit on success, rollback on error
                conn.execute(
                    "INSERT INTO cost_ledger("
                    "ts, model, input_tokens, output_tokens, "
                    "est_cost_usd, request_kind) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        ts,
                        model,
                        int(input_tokens),
                        int(output_tokens),
                        float(cost),
                        request_kind,
                    ),
                )
        finally:
            conn.close()

        self._maybe_soft_alarm()
        return cost

    # -------------------------------------------------------------- internals

    def _maybe_soft_alarm(self) -> None:
        """Emit a stderr warning if monthly total has crossed the soft-alarm
        fraction of the cap. Fires at most once per process run.
        """
        if self._soft_alarm_fired:
            return
        total = self.monthly_total()
        threshold = _SOFT_ALARM_FRACTION * self._cap
        if total >= threshold:
            self._soft_alarm_fired = True
            print(
                f"warning: LLM monthly spend ${total:.4f} has crossed "
                f"{int(_SOFT_ALARM_FRACTION * 100)}% of the "
                f"${self._cap:.2f} cap. Consider tightening usage.",
                file=sys.stderr,
            )

    # Test/utility hook; not part of the documented API surface.
    def _reset_soft_alarm_for_tests(self) -> None:
        self._soft_alarm_fired = False
