"""tm suggest CLI: run SchedulerAgent for a case date."""

from __future__ import annotations

from typing import Annotated

import typer

from tm._paths import default_db_path
from tm.agents.scheduler import (
    ScheduledSuggestion,
    SchedulerAgent,
    SchedulerSkipReason,
)
from tm.commands._shared import (
    DbPathOption,
    ensure_migrations,
    require_api_key,
    utc_today,
)
from tm.engines.process_mining import ProcessMiner
from tm.engines.variant_cluster import VariantClusterer
from tm.llm.anthropic_adapter import (
    DEFAULT_MAX_TOKENS,
    DEFAULT_MODEL,
    AnthropicAdapter,
)
from tm.llm.cost_meter import CostMeter
from tm.models.outcome import OutcomeAggregator
from tm.repositories.events import EventsRepository
from tm.repositories.goals import GoalsRepository
from tm.repositories.telemetry import SuggestionTelemetryRepository

suggest_app = typer.Typer(help="Generate one proactive scheduler suggestion.")

DEFAULT_SUGGEST_COST_CAP_USD = 0.25


def _format_optional_float(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.2f}"


def _render_suggestion(result: ScheduledSuggestion) -> None:
    candidate = result.candidate
    conformance_deviation: float | None
    if candidate.predicted_post_suggestion_fitness is None:
        conformance_deviation = None
    else:
        conformance_deviation = 1.0 - float(candidate.predicted_post_suggestion_fitness)

    typer.echo(f"Suggestion ready (suggestion_id={result.suggestion_id})")
    typer.echo(f"Recommended action: {candidate.recommended_action}")
    typer.echo(f"Predicted outcome WITH:    {candidate.predicted_outcome_with:.2f}")
    typer.echo(f"Predicted outcome WITHOUT: {candidate.predicted_outcome_without:.2f}")
    typer.echo(
        f"Predicted delta:           {result.guardrails.predicted_outcome_delta:+.2f}"
    )
    typer.echo(
        f"Conformance deviation:     {_format_optional_float(conformance_deviation)}"
    )
    typer.echo(f"LLM rationale: {candidate.explanation}")


def _render_skip(result: SchedulerSkipReason) -> None:
    typer.echo(f"Suggestion skipped: {result.reason}")
    typer.echo(f"Detail: {result.detail}")


@suggest_app.callback(invoke_without_command=True)
def suggest(
    db_path: DbPathOption = None,
    case_date: Annotated[
        str | None,
        typer.Option(
            "--case-date",
            help="Case date to suggest against (YYYY-MM-DD). Defaults to today UTC.",
        ),
    ] = None,
    case_goal_id: Annotated[
        str | None,
        typer.Option("--case-goal-id", help="Optional goal-pursuit lens ULID."),
    ] = None,
    model: Annotated[
        str,
        typer.Option("--model", help="Anthropic model name."),
    ] = DEFAULT_MODEL,
    monthly_cap_usd: Annotated[
        float,
        typer.Option(
            "--monthly-cap-usd",
            help=(
                "Monthly cost cap in USD. The command refuses to run if "
                "cumulative monthly spend would exceed this."
            ),
        ),
    ] = DEFAULT_SUGGEST_COST_CAP_USD,
    max_per_day: Annotated[
        int | None,
        typer.Option("--max-per-day", help="Maximum suggestions allowed per day."),
    ] = None,
) -> None:
    """Generate and persist one scheduler suggestion, or explain the skip."""
    require_api_key("tm suggest")

    resolved_db_path = db_path or default_db_path()
    resolved_case_date = case_date or utc_today()

    ensure_migrations(resolved_db_path)
    llm = AnthropicAdapter(model=model, max_tokens=DEFAULT_MAX_TOKENS)
    events_repo = EventsRepository(resolved_db_path)
    goals_repo = GoalsRepository(resolved_db_path)
    outcome_aggregator = OutcomeAggregator(events_repo)
    process_miner = ProcessMiner(events_repo)
    variant_clusterer = VariantClusterer(events_repo, outcome_aggregator)
    telemetry_repo = SuggestionTelemetryRepository(resolved_db_path)
    cost_meter = CostMeter(resolved_db_path, monthly_cap_usd=monthly_cap_usd)

    agent = SchedulerAgent(
        llm_client=llm,
        process_miner=process_miner,
        variant_clusterer=variant_clusterer,
        outcome_aggregator=outcome_aggregator,
        telemetry_repo=telemetry_repo,
        events_repo=events_repo,
        goals_repo=goals_repo,
        cost_meter=cost_meter,
        max_proactive_per_day=max_per_day,
        model=model,
        max_tokens=DEFAULT_MAX_TOKENS,
    )
    result = agent.propose_suggestion(
        case_date=resolved_case_date,
        case_goal_id=case_goal_id,
    )
    if isinstance(result, SchedulerSkipReason):
        _render_skip(result)
    else:
        _render_suggestion(result)
