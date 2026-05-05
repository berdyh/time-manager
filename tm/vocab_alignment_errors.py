"""Typed exception(s) for vocabulary alignment.

Kept in its own module so callers (and future CLI code in T-VOC-03) can
import the exception type without pulling in the full alignment machinery.
"""

from __future__ import annotations

__all__ = ["AlignmentError"]


class AlignmentError(RuntimeError):
    """Raised when an LLM alignment response fails validation.

    Examples
    --------
    * Missing required keys (``canonical``, ``confidence``, ``is_novel``,
      ``reason``).
    * ``confidence`` outside the closed interval ``[0.0, 1.0]``.
    * Wrong types on any field.
    * Empty ``reason`` string.

    Hallucinated canonicals (LLM proposes a canonical name not present in
    vocabulary) are NOT raised as errors — the aligner downgrades those to
    ``canonical=None, is_novel=True`` instead, since that is a recoverable
    misalignment rather than a malformed response.
    """
