"""Task taxonomy. Adding a task type is a data change here, not a router change.

Each task type says: what family of work it is, its default difficulty band,
which validator can check the answer deterministically (if any), which model
capabilities it needs, how many output tokens it deserves, whether it is
batch-eligible, and whether it is high-stakes.
"""
from dataclasses import dataclass, field

DIFFICULTY_RANK = {"SIMPLE": 1, "MEDIUM": 2, "HARD": 3}


@dataclass(frozen=True)
class TaskType:
    name: str
    label: str
    family: str                      # lookup | analysis | generation | classification | extraction | summarization | qa
    default_difficulty: str
    validator: str | None = None     # label | json_schema | sql | groundedness | None (judge only)
    required_capabilities: tuple = ()
    output_budget_tokens: int = 400
    reasoning: str = "low"           # suggested reasoning level for models with controls
    batch_eligible: bool = False
    high_stakes: bool = False
    context_sections: tuple = ("metrics", "incidents")   # what the optimizer keeps for this task
    description: str = ""


TASK_TYPES = {t.name: t for t in [
    TaskType("metric_lookup", "Metric lookup", "lookup", "SIMPLE", "groundedness",
             output_budget_tokens=120, context_sections=("metrics",),
             description="A single current value from telemetry."),
    TaskType("health_check", "Health check", "lookup", "SIMPLE", "groundedness",
             output_budget_tokens=200, context_sections=("metrics", "incidents", "logs")),
    TaskType("count_aggregate", "Count / aggregate", "lookup", "SIMPLE", "groundedness",
             output_budget_tokens=200, context_sections=("metrics", "incidents", "logs")),
    TaskType("incident_lookup", "Incident field lookup", "lookup", "SIMPLE", "groundedness",
             output_budget_tokens=80, context_sections=("incidents",)),
    TaskType("alert_classification", "Alert classification", "classification", "SIMPLE", "label",
             output_budget_tokens=20, batch_eligible=True, context_sections=()),
    TaskType("log_extraction", "Structured log extraction", "extraction", "SIMPLE", "json_schema",
             required_capabilities=("json_mode",), output_budget_tokens=160, batch_eligible=True,
             context_sections=()),
    TaskType("sql_generation", "SQL generation", "generation", "MEDIUM", "sql",
             output_budget_tokens=200, reasoning="medium", context_sections=("schema",)),
    TaskType("docs_qa", "Documentation Q&A", "qa", "SIMPLE", "groundedness",
             output_budget_tokens=250, context_sections=("runbooks",)),
    TaskType("ticket_summary", "Ticket summarization", "summarization", "MEDIUM", None,
             output_budget_tokens=220, batch_eligible=True, context_sections=("tickets",)),
    TaskType("incident_summary", "Incident summary", "summarization", "MEDIUM", "groundedness",
             output_budget_tokens=260, context_sections=("metrics", "incidents", "logs")),
    TaskType("diagnosis", "Diagnosis (why)", "analysis", "MEDIUM", "groundedness",
             output_budget_tokens=350, reasoning="medium", context_sections=("metrics", "incidents", "logs")),
    TaskType("comparison", "Comparison", "analysis", "MEDIUM", "groundedness",
             output_budget_tokens=250, context_sections=("metrics", "incidents")),
    TaskType("trend_analysis", "Trend analysis", "analysis", "MEDIUM", "groundedness",
             output_budget_tokens=300, reasoning="medium", context_sections=("metrics", "incidents", "logs")),
    TaskType("correlation", "Correlation", "analysis", "MEDIUM", "groundedness",
             output_budget_tokens=350, reasoning="medium", context_sections=("metrics", "incidents", "logs")),
    TaskType("root_cause_analysis", "Root-cause analysis", "analysis", "HARD", "groundedness",
             output_budget_tokens=600, reasoning="high", high_stakes=True,
             context_sections=("metrics", "incidents", "logs")),
    TaskType("timeline", "Event timeline", "analysis", "HARD", "groundedness",
             output_budget_tokens=600, reasoning="high", context_sections=("metrics", "incidents", "logs")),
    TaskType("remediation", "Remediation plan", "analysis", "HARD", None,
             output_budget_tokens=700, reasoning="high", high_stakes=True,
             context_sections=("metrics", "incidents", "logs", "runbooks")),
    TaskType("impact_assessment", "Impact assessment", "analysis", "HARD", "groundedness",
             output_budget_tokens=600, reasoning="high", high_stakes=True,
             context_sections=("metrics", "incidents", "logs")),
]}

FAMILIES = sorted({t.family for t in TASK_TYPES.values()})


def get(name: str | None) -> TaskType:
    return TASK_TYPES.get(name or "") or TASK_TYPES["diagnosis"]


def public() -> list:
    return [dict(vars(t)) for t in TASK_TYPES.values()]
