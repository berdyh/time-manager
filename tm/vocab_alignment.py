"""Vocabulary alignment manager (T-VOC-02).

The :class:`VocabAligner` glues together :class:`VocabularyRepository`
(T-VOC-01) and an :class:`LLMClient` (T-FND-04) to provide three operations
used by the time-manager governance loop:

1. :meth:`VocabAligner.align` — soft-aligns a free-text label to a canonical
   activity, falling back to the LLM when the repository has neither a
   direct vocabulary hit nor an alias hit.
2. :meth:`VocabAligner.compute_novelty_rate` — fraction of events in a
   window whose ``activity`` is not in the active vocabulary.
3. :meth:`VocabAligner.find_drifted_activities` — canonical activities
   that have not been observed in the events log for at least ``idle_days``.

This module is read-only with respect to the ``vocabulary``, ``aliases`` and
``events`` tables. Mutations to those tables are owned by other workers
(T-VOC-01 for vocab/aliases via :class:`VocabularyRepository`, T-PM-01 /
extractors for events). The CLI surface (``tm vocab review``) is owned by
T-VOC-03 and lives in a separate module.

Test contract: NO live LLM calls. Tests inject a ``Mock`` for the
:class:`LLMClient` and a fresh in-memory or ``tmp_path`` SQLite database
with all migrations applied.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from tm.llm.client import Message
from tm.vocab_alignment_errors import AlignmentError

if TYPE_CHECKING:  # pragma: no cover
    from tm.llm.client import LLMClient
    from tm.repositories.vocabulary import VocabularyRepository

__all__ = [
    "ALIGNMENT_PROMPT",
    "ALIGNMENT_SCHEMA",
    "AlignmentResult",
    "VocabAligner",
    "lower_normalize",
]


# ---------------------------------------------------------------------------
# Prompt & schema constants
# ---------------------------------------------------------------------------

ALIGNMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "canonical": {
            "type": ["string", "null"],
            "description": (
                "canonical activity name from the supplied vocabulary, or "
                "null if no good match exists"
            ),
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "is_novel": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["canonical", "confidence", "is_novel", "reason"],
}
"""JSON schema enforced by :meth:`VocabAligner.align` on the LLM response.

The :class:`tm.llm.anthropic_adapter.AnthropicAdapter` plumbs this through
provider-side tool use, but T-FND-04 does not validate the returned dict
against the schema — :class:`VocabAligner` does that itself.
"""

ALIGNMENT_PROMPT: str = (
    "You are aligning a free-text activity label to a fixed canonical "
    "vocabulary. Given the user-provided label, choose the closest "
    "canonical name from the supplied list ONLY when your confidence is at "
    "least 0.6; otherwise return canonical=null and is_novel=true. Never "
    "invent a canonical name that is not in the supplied list. The 'reason' "
    "field must be a short non-empty rationale for your choice."
)
"""System prompt fed to :meth:`tm.llm.client.LLMClient.extract`."""

# Confidence floor below which the LLM should report no match. The aligner
# does NOT enforce this server-side (the LLM is instructed to apply it); we
# keep the constant exposed so the prompt and any future callers stay in sync.
_MIN_MATCH_CONFIDENCE: float = 0.6

# Confidence we report for a direct repository hit (vocab or alias).
_REPO_HIT_CONFIDENCE: float = 1.0


# ---------------------------------------------------------------------------
# Public dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AlignmentResult:
    """Outcome of a single alignment attempt.

    Attributes
    ----------
    canonical:
        Resolved canonical activity name from ``vocabulary``, or ``None`` if
        the LLM (or repository) could not confidently match.
    confidence:
        Float in ``[0.0, 1.0]``. Direct vocabulary / alias hits report
        ``1.0``; LLM proposals echo whatever confidence the model returned.
    is_novel:
        ``True`` when the label is judged to be a new activity not covered
        by the active vocabulary. Always ``False`` for repo hits.
    reason:
        Short rationale. ``"vocab/alias hit"`` for repo hits; LLM-supplied
        text otherwise. Never empty.
    raw_input:
        The original free-text label as passed in.
    normalized_input:
        ``lower_normalize(raw_input)`` — what was used for the lookup.
    """

    canonical: str | None
    confidence: float
    is_novel: bool
    reason: str
    raw_input: str
    normalized_input: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def lower_normalize(s: str) -> str:
    """Strip surrounding whitespace and lowercase the input.

    This is the canonical normalization function used by
    :meth:`VocabAligner.align` and recommended for any caller that wants to
    persist an alias proposed by the LLM (``add_alias`` does NOT normalize
    its inputs — see T-VOC-01 carry-forward intel).
    """
    return s.strip().lower()


def _utc_now_iso() -> str:
    """Return a timezone-aware UTC timestamp matching the events table format.

    Format mirrors :mod:`tm.repositories.events` (ISO 8601 with trailing
    ``Z``) for lexicographic comparability.
    """
    now = datetime.now(UTC).replace(microsecond=0)
    return now.strftime("%Y-%m-%dT%H:%M:%SZ")


def _subtract_days(iso_ts: str, days: int) -> str:
    """Return ``iso_ts - days`` as an ISO 8601 ``...Z`` string.

    Tolerates both ``Z``-suffixed and ``+00:00``-suffixed inputs.
    """
    cleaned = iso_ts.replace("Z", "+00:00")
    dt = datetime.fromisoformat(cleaned)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    cut = (dt - timedelta(days=days)).astimezone(UTC)
    return cut.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Aligner
# ---------------------------------------------------------------------------


class VocabAligner:
    """Soft-align free-text labels to canonical vocabulary entries.

    Parameters
    ----------
    vocab_repo:
        :class:`tm.repositories.vocabulary.VocabularyRepository` instance.
        Used both for the fast resolve path and to enumerate active
        canonicals when assembling the LLM prompt and validating output.
    llm:
        Optional :class:`tm.llm.client.LLMClient` implementation.  Drift and
        novelty checks do not need it.  Tests pass a ``unittest.mock.Mock``;
        production wires in
        :class:`tm.llm.anthropic_adapter.AnthropicAdapter`.
    model:
        Reserved for future use (e.g. to thread a model id through to the
        LLM call).  Currently informational only — the v1 adapter resolves
        the model from its own constructor (see T-FND-04 carry-forward
        intel about per-call overrides not being honoured).

    Notes
    -----
    * No retry / backoff is added on top of the LLM client.  Transient SDK
      errors propagate as-is per T-FND-04 contract.
    * The aligner reads the ``events`` table directly via :mod:`sqlite3` to
      keep schema coupling minimal; only the stable ``activity`` and
      ``timestamp`` columns are touched.
    """

    def __init__(
        self,
        vocab_repo: VocabularyRepository,
        llm: LLMClient | None = None,
        *,
        model: str | None = None,
    ) -> None:
        self._vocab_repo = vocab_repo
        self._llm = llm
        self._model = model
        self._db_path = vocab_repo.db_path

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def align(self, free_text: str) -> AlignmentResult:
        """Resolve ``free_text`` to a canonical activity.

        Order of operations:

        1. Normalize input via :func:`lower_normalize`.
        2. Try :meth:`VocabularyRepository.resolve` — on hit, return a
           confidence-1.0 result; the LLM is NOT consulted.
        3. Otherwise call :meth:`LLMClient.extract` against
           :data:`ALIGNMENT_SCHEMA`, validate the response, and reject any
           hallucinated canonicals (LLM-proposed names not in the active
           vocabulary).

        Raises
        ------
        AlignmentError
            On malformed LLM output (missing keys, wrong types, out-of-range
            confidence, empty reason).
        """
        raw = free_text
        normalized = lower_normalize(free_text)

        # 1. Fast path: direct vocab or alias hit.
        canonical = self._vocab_repo.resolve(normalized)
        if canonical is not None:
            return AlignmentResult(
                canonical=canonical,
                confidence=_REPO_HIT_CONFIDENCE,
                is_novel=False,
                reason="vocab/alias hit",
                raw_input=raw,
                normalized_input=normalized,
            )

        # 2. Slow path: ask the LLM.
        active_names = {e.activity_name for e in self._vocab_repo.list_active()}
        prompt = self._build_user_prompt(normalized, sorted(active_names))
        if self._llm is None:
            raise AlignmentError("LLM client is required for novel label alignment")
        response = self._llm.extract(
            messages=[Message(role="user", content=prompt)],
            schema=ALIGNMENT_SCHEMA,
        )
        validated = _validate_llm_response(response)

        # 3. Hallucination guard: if LLM proposed a canonical we don't know,
        # downgrade to a novel-flagged null result rather than passing the
        # phantom name through to callers.
        proposed = validated["canonical"]
        if proposed is not None and proposed not in active_names:
            return AlignmentResult(
                canonical=None,
                confidence=float(validated["confidence"]),
                is_novel=True,
                reason=f'LLM proposed unknown canonical "{proposed}"',
                raw_input=raw,
                normalized_input=normalized,
            )

        return AlignmentResult(
            canonical=proposed,
            confidence=float(validated["confidence"]),
            is_novel=bool(validated["is_novel"]),
            reason=str(validated["reason"]),
            raw_input=raw,
            normalized_input=normalized,
        )

    def compute_novelty_rate(
        self,
        since: str | None = None,
        until: str | None = None,
    ) -> float:
        """Fraction of events in ``[since, until)`` outside the active vocab.

        Parameters
        ----------
        since:
            ISO 8601 lower bound (inclusive).  ``None`` removes the lower
            bound (events are read from the beginning of the log).
        until:
            ISO 8601 upper bound (exclusive).  ``None`` removes the upper
            bound (events are read up to "now").

        Returns
        -------
        float
            ``0.0`` when the window is empty.  Otherwise
            ``count_unknown / total`` in ``[0.0, 1.0]``.

        Notes
        -----
        Compares against :meth:`VocabularyRepository.list_active` (archived
        canonicals are treated as unknown) so that tombstoned activities
        contribute to the novelty signal — desired behaviour for governance
        review where archived names may need to be re-introduced.
        """
        active_names = {e.activity_name for e in self._vocab_repo.list_active()}

        sql = "SELECT activity FROM events"
        clauses: list[str] = []
        params: list[Any] = []
        if since is not None:
            clauses.append("timestamp >= ?")
            params.append(since)
        if until is not None:
            clauses.append("timestamp < ?")
            params.append(until)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)

        conn = sqlite3.connect(self._db_path)
        try:
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()

        if not rows:
            return 0.0

        unknown = sum(1 for (activity,) in rows if activity not in active_names)
        return unknown / len(rows)

    def find_drifted_activities(
        self,
        idle_days: int = 30,
        *,
        as_of: str | None = None,
    ) -> list[str]:
        """Active canonicals with no events in the past ``idle_days`` days.

        Parameters
        ----------
        idle_days:
            Threshold in days. Default 30. The ``tm vocab drift`` CLI
            overrides this to 14 at the call site.
        as_of:
            ISO 8601 anchor timestamp (defaults to now-UTC).  An activity
            counts as "drifted" if it has no events with
            ``timestamp >= as_of - idle_days``.

        Returns
        -------
        list[str]
            Canonical names sorted in the same order as
            :meth:`VocabularyRepository.list_active` (alphabetical).
            Archived canonicals are excluded.
        """
        anchor = as_of if as_of is not None else _utc_now_iso()
        cutoff = _subtract_days(anchor, idle_days)
        active_names = [e.activity_name for e in self._vocab_repo.list_active()]

        if not active_names:
            return []

        drifted: list[str] = []
        conn = sqlite3.connect(self._db_path)
        try:
            for name in active_names:
                row = conn.execute(
                    "SELECT 1 FROM events "
                    "WHERE activity = ? AND timestamp >= ? LIMIT 1",
                    (name, cutoff),
                ).fetchone()
                if row is None:
                    drifted.append(name)
        finally:
            conn.close()
        return drifted

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _build_user_prompt(
        self,
        normalized_label: str,
        active_names: list[str],
    ) -> str:
        """Assemble the user-message body fed to the LLM.

        Combines the standing system-style instructions in
        :data:`ALIGNMENT_PROMPT`, the active vocabulary list, and the
        normalized label.  Kept tight on purpose — v1 spec calls for a
        single-paragraph prompt plus the bullet list.
        """
        bullets = "\n".join(f"- {name}" for name in active_names)
        return (
            f"{ALIGNMENT_PROMPT}\n\n"
            f"Active canonical vocabulary:\n{bullets}\n\n"
            f"Free-text label to align: {normalized_label!r}"
        )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _validate_llm_response(response: Any) -> dict[str, Any]:
    """Validate the dict returned by :meth:`LLMClient.extract`.

    Raises :class:`AlignmentError` with a precise message on any violation.
    Returns the same dict on success (no copy / coercion beyond what the
    aligner does at the call site).
    """
    if not isinstance(response, dict):
        raise AlignmentError(
            f"alignment response must be a dict, got {type(response).__name__}"
        )

    required = ("canonical", "confidence", "is_novel", "reason")
    missing = [k for k in required if k not in response]
    if missing:
        raise AlignmentError(
            f"alignment response missing required keys: {sorted(missing)!r}"
        )

    canonical = response["canonical"]
    if canonical is not None and not isinstance(canonical, str):
        raise AlignmentError(
            "alignment response 'canonical' must be str or None, got "
            f"{type(canonical).__name__}"
        )

    confidence = response["confidence"]
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise AlignmentError(
            "alignment response 'confidence' must be a number, got "
            f"{type(confidence).__name__}"
        )
    if not (0.0 <= float(confidence) <= 1.0):
        raise AlignmentError(
            f"alignment response 'confidence' out of [0,1] range: {confidence!r}"
        )

    is_novel = response["is_novel"]
    if not isinstance(is_novel, bool):
        raise AlignmentError(
            f"alignment response 'is_novel' must be bool, got {type(is_novel).__name__}"
        )

    reason = response["reason"]
    if not isinstance(reason, str) or not reason.strip():
        raise AlignmentError("alignment response 'reason' must be a non-empty string")

    return response
