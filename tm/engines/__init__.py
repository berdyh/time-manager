"""Process mining + analysis engines.

Currently exports :class:`ProcessMiner` and its public dataclasses, the
:class:`VariantClusterer` (T-OUT-03) that labels mined variants by mean
outcome score, and the :class:`SchedulerSuccessMetric` (T-OUT-03) that
aggregates suggestion telemetry against actual outcomes.  Other engines
(e.g. outcome aggregation, future planners) live in their own modules and
are imported on demand.
"""

from tm.engines.prescriptive_monitoring import (
    DEFAULT_CONFORMANCE_FITNESS_FLOOR,
    DEFAULT_COUNTERFACTUAL_DELTA_THRESHOLD,
    CandidateSuggestion,
    ConformanceDeviationGuard,
    CounterfactualGuard,
    Guardrails,
    GuardrailsEvaluation,
    GuardrailVerdict,
    ObjectiveFunctionGuard,
)
from tm.engines.process_mining import (
    ConformanceResult,
    DiscoveredModel,
    PerformanceAnalysis,
    PerformanceMetric,
    ProcessMiner,
    Variant,
    VariantAnalysis,
)
from tm.engines.scheduler_metric import (
    SchedulerMetricSummary,
    SchedulerSuccessMetric,
)
from tm.engines.variant_cluster import (
    BAD_DAY_THRESHOLD,
    EFFECTIVE_OUTCOME_THRESHOLD,
    EFFECTIVE_THROUGHPUT_MAX,
    GOOD_DAY_THRESHOLD,
    LabeledVariant,
    VariantClusterer,
    VariantClustering,
)

__all__ = [
    "BAD_DAY_THRESHOLD",
    "DEFAULT_CONFORMANCE_FITNESS_FLOOR",
    "DEFAULT_COUNTERFACTUAL_DELTA_THRESHOLD",
    "EFFECTIVE_OUTCOME_THRESHOLD",
    "EFFECTIVE_THROUGHPUT_MAX",
    "GOOD_DAY_THRESHOLD",
    "CandidateSuggestion",
    "ConformanceDeviationGuard",
    "ConformanceResult",
    "CounterfactualGuard",
    "DiscoveredModel",
    "GuardrailVerdict",
    "Guardrails",
    "GuardrailsEvaluation",
    "LabeledVariant",
    "ObjectiveFunctionGuard",
    "PerformanceAnalysis",
    "PerformanceMetric",
    "ProcessMiner",
    "SchedulerMetricSummary",
    "SchedulerSuccessMetric",
    "Variant",
    "VariantAnalysis",
    "VariantClusterer",
    "VariantClustering",
]
