"""Execution planner: mode, reasoning level, output budget.

Mode comes from the policy's SLA class (and the task's batch eligibility when
the policy says auto). Reasoning level is a policy dimension for models that
expose controls. Output budget follows the task type unless the policy fixes
it. Every choice is explained.
"""
from dataclasses import dataclass, field

from backend.optimizer import tasks
from backend.optimizer.registry import ModelSpec

REASONING_LEVELS = ("low", "medium", "high")
MODE_FOR_SLA = {"interactive": "interactive", "near_realtime": "near_realtime",
                "batch": "batch", "offline": "offline"}


@dataclass
class ExecutionPlan:
    execution_mode: str
    reasoning_level: str | None
    reasoning_default: str | None
    reasoning_reduced: bool
    output_budget_tokens: int
    request_params: dict
    reasons: list = field(default_factory=list)
    batch_discount: float | None = None

    def public(self) -> dict:
        return {"execution_mode": self.execution_mode, "reasoning_level": self.reasoning_level,
                "reasoning_default": self.reasoning_default, "reasoning_reduced": self.reasoning_reduced,
                "output_budget_tokens": self.output_budget_tokens, "batch_discount": self.batch_discount,
                "reasons": self.reasons}


def plan(model: ModelSpec, task_type: str, difficulty: str, policy, registry, *,
         batch_enabled: bool = True, reasoning_enabled: bool = True,
         answer_max_tokens: int = 900,
         batch_disabled_reason: str = "batch optimisation flag off") -> ExecutionPlan:
    task = tasks.get(task_type)
    reasons = []
    # ------------------------------------------------------------ mode
    sla = policy.get("sla_class", "interactive")
    mode_policy = policy.get("execution_mode_policy", "auto")
    if mode_policy == "force_interactive":
        mode, why = "interactive", "policy forces interactive execution"
    elif mode_policy == "force_batch":
        mode, why = "batch", "policy forces batch execution"
    else:
        mode = MODE_FOR_SLA.get(sla, "interactive")
        why = f"SLA class {sla} ({policy.source('sla_class')})"
        if mode == "interactive" and task.batch_eligible and sla == "near_realtime":
            mode, why = "batch", f"task {task.name} is batch-eligible and SLA {sla} tolerates it"
    if not batch_enabled and mode in ("batch", "offline"):
        reasons.append(f"{batch_disabled_reason}: executing interactively at standard rates")
        mode = "interactive"
    reasons.append(f"execution mode {mode}: {why}")
    discount = registry.provider(model.provider).batch_discount if mode in ("batch", "offline") else None
    if mode in ("batch", "offline"):
        reasons.append(f"{model.provider} batch discount: "
                       + (f"{int(discount * 100)}% (modeled; this demo executes synchronously)" if discount
                          else "none published, standard rate"))
    # ------------------------------------------------------- reasoning
    level, default, reduced = None, model.reasoning_default, False
    if model.supports("reasoning_controls"):
        rp = policy.get("reasoning_policy", "adaptive")
        if not reasoning_enabled or rp == "model_default":
            level = default
            reasons.append(f"reasoning {level}: model default" + ("" if reasoning_enabled else " (flag off)"))
        elif rp == "minimum":
            level = "low"
            reasons.append("reasoning low: policy minimum")
        else:
            level = task.reasoning if difficulty != "SIMPLE" else "low"
            if difficulty == "HARD" and level == "low":
                level = "medium"
            reasons.append(f"reasoning {level}: adaptive for {task.name} / {difficulty}")
        reduced = bool(default and level and REASONING_LEVELS.index(level) < REASONING_LEVELS.index(default))
    else:
        reasons.append("reasoning: model exposes no controls")
    # --------------------------------------------------- output budget
    if policy.get("output_budget_policy", "adaptive") == "adaptive":
        visible = task.output_budget_tokens
        # Reasoning models spend hidden tokens out of the same completion budget.
        # Measured on the gpt-oss family: ~25-30 tokens at "low", 170-215 at the
        # default. A budget that ignores them truncates the visible answer to
        # nothing, the call is treated as a failure, and the request fails over
        # to a more expensive model - costing more than never having capped it.
        headroom = 0
        if model.supports("reasoning_controls"):
            measured = model.reasoning_tokens_measured or {}
            headroom = int(measured.get(level or "low", measured.get("default", 0)) or 0)
            headroom = int(headroom * 1.6) + 24        # margin: the figure is a mean, not a ceiling
        elif level == "high":
            headroom = 300
        elif level == "medium":
            headroom = 150
        budget = min(answer_max_tokens, visible + headroom)
        if headroom:
            reasons.append(f"output budget {budget} tokens for {task.name}: {visible} visible plus "
                           f"{headroom} for hidden reasoning at level {level or 'low'} "
                           f"({model.reasoning_tokens_measured.get('source', 'estimated')})")
        else:
            reasons.append(f"output budget {budget} tokens for {task.name}")
    else:
        budget = answer_max_tokens
        reasons.append(f"output budget {budget}: policy fixed")
    params = dict(model.params)
    if level and model.supports("reasoning_controls"):
        params["reasoning_effort"] = level
    return ExecutionPlan(mode, level, default, reduced, budget, params, reasons, discount)
