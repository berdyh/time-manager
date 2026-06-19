"""tm debrief CLI: run DebriefAgent against a transcript."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from tm.agents.debrief import DebriefAgent, DebriefResult, DuplicateSummaryError
from tm.commands._shared import (
    DbPathOption,
    cli_error,
    prepare_db,
    read_text_file,
    require_api_key,
    utc_today,
    validate_case_date,
)
from tm.llm.anthropic_adapter import (
    DEFAULT_MAX_TOKENS,
    DEFAULT_MODEL,
)
from tm.llm.cost_meter import CostMeter
from tm.llm.factory import build_llm_client
from tm.repositories.events import EventsRepository
from tm.repositories.goals import GoalsRepository
from tm.repositories.transcripts import TranscriptRepository
from tm.repositories.vocabulary import VocabularyRepository
from tm.vocab_alignment import VocabAligner

debrief_app = typer.Typer(help="Run LLM debrief extraction from a transcript.")

DEFAULT_DEBRIEF_COST_CAP_USD = 0.50


def _read_transcript(
    *,
    transcript_file: Path | None,
    from_stdin: bool,
) -> str:
    has_file = transcript_file is not None
    if has_file == from_stdin:
        cli_error(
            "exactly one input source is required: --transcript-file or --from-stdin",
            code=2,
        )

    if from_stdin:
        return typer.get_text_stream("stdin").read()

    if transcript_file is None:  # pragma: no cover - guarded above
        raise typer.Exit(2)
    return read_text_file(transcript_file, "transcript file")


def _format_summary(summary: dict[str, int]) -> str:
    preferred = ("planned_tasks_completed", "planned_tasks_total")
    parts: list[str] = []
    seen: set[str] = set()
    for key in preferred:
        if key in summary:
            parts.append(f"{key}: {summary[key]}")
            seen.add(key)
    for key in sorted(set(summary) - seen):
        parts.append(f"{key}: {summary[key]}")
    return "{" + ", ".join(parts) + "}"


def render_debrief_result(result: DebriefResult) -> None:
    typer.echo(f"Debrief complete: case_date={result.case_date}")
    typer.echo(f"Events persisted: {result.events_persisted}")
    typer.echo(
        f"Summary event persisted: {'yes' if result.summary_event_persisted else 'no'}"
    )
    typer.echo(f"Novel labels: {list(result.novel_labels)!r}")
    typer.echo(f"Summary: {_format_summary(result.summary)}")
    typer.echo(
        f"Cost (estimated): ${result.cost_estimated_usd:.3f}"
        f"   |   Cost (actual): ${result.cost_actual_usd:.3f}"
    )
    typer.echo(f"Extractor version: {result.extractor_version}")
    if not result.extracted_events:
        typer.echo("No activity events extracted from transcript.")


def build_debrief_agent(
    *,
    db_path: Path,
    model: str,
    max_tokens: int,
    monthly_cap_usd: float,
) -> DebriefAgent:
    llm = build_llm_client(model=model, max_tokens=max_tokens)
    vocab_repo = VocabularyRepository(db_path)
    return DebriefAgent(
        llm_client=llm,
        vocab_aligner=VocabAligner(vocab_repo, llm),
        goals_repo=GoalsRepository(db_path),
        events_repo=EventsRepository(db_path),
        cost_meter=CostMeter(db_path, monthly_cap_usd=monthly_cap_usd),
        model=model,
        max_tokens=max_tokens,
    )


@debrief_app.callback(invoke_without_command=True)
def debrief(
    db_path: DbPathOption = None,
    case_date: Annotated[
        str | None,
        typer.Option(
            "--case-date",
            help="Case date to persist against (YYYY-MM-DD). Defaults to today UTC.",
        ),
    ] = None,
    transcript_file: Annotated[
        Path | None,
        typer.Option("--transcript-file", help="Read transcript text from PATH."),
    ] = None,
    from_stdin: Annotated[
        bool,
        typer.Option("--from-stdin", help="Read transcript text from stdin."),
    ] = False,
    model: Annotated[
        str,
        typer.Option("--model", help="Anthropic model name."),
    ] = DEFAULT_MODEL,
    max_tokens: Annotated[
        int,
        typer.Option("--max-tokens", help="Maximum output tokens for extraction."),
    ] = DEFAULT_MAX_TOKENS,
    monthly_cap_usd: Annotated[
        float,
        typer.Option(
            "--monthly-cap-usd",
            help=(
                "Monthly cost cap in USD. The command refuses to run if "
                "cumulative monthly spend would exceed this."
            ),
        ),
    ] = DEFAULT_DEBRIEF_COST_CAP_USD,
) -> None:
    """Extract structured events from a transcript and persist them."""
    transcript = _read_transcript(
        transcript_file=transcript_file,
        from_stdin=from_stdin,
    )
    require_api_key("tm debrief")

    resolved_case_date = validate_case_date(case_date or utc_today())
    resolved_db_path = prepare_db(db_path)

    agent = build_debrief_agent(
        db_path=resolved_db_path,
        model=model,
        max_tokens=max_tokens,
        monthly_cap_usd=monthly_cap_usd,
    )
    try:
        result = agent.extract_and_persist(
            transcript=transcript,
            case_date=resolved_case_date,
        )
    except DuplicateSummaryError as exc:
        # Race-induced single-summary collision (post-/simplify the daemon's
        # coarse write lock no longer serialises LLM-backed handlers, so two
        # concurrent ``run_debrief`` RPCs for the same case_date can both
        # pass the pre-call SELECT and then collide at INSERT — caught by
        # the partial UNIQUE index added in migration 0010). The CLI is the
        # operator boundary: render a friendly message and exit 1 rather
        # than let the exception traceback through.
        typer.echo(
            f"Debrief skipped: a summary already exists for "
            f"case_date={exc.case_date}. Use 'tm reextract' (v1.1) to "
            f"replace it, or remove the existing summary manually.",
            err=True,
        )
        raise typer.Exit(1) from exc
    TranscriptRepository(resolved_db_path).upsert(
        case_date=resolved_case_date,
        transcript_text=transcript,
        source="debrief",
        extractor_version="debrief-v1",
    )
    render_debrief_result(result)
