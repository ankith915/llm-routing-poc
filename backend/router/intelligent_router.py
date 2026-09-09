"""Strategy C: cost-aware intelligent routing.

A small LLM analyzes the query and produces a routing decision; the code then
selects the CHEAPEST configured model that satisfies every stated requirement.
Falls back to the rule router if the router call or JSON parsing fails.
"""
import json
import re

from backend.config.loader import Settings, TIER_RANK
from backend.llm.client import ClientPool, LLMError
from backend.router import rule_router

ROUTER_PROMPT = """You are a routing controller for an IT-operations LLM assistant.
Classify the incoming user query. Respond with ONLY a JSON object, no prose:

{{
  "complexity": "SIMPLE" | "MEDIUM" | "HARD",
  "reasoning_required": true | false,
  "context_requirement": "LOW" | "MEDIUM" | "HIGH",
  "quality_requirement": "LOW" | "MEDIUM" | "HIGH" | "CRITICAL",
  "latency_requirement": "LOW" | "MEDIUM" | "HIGH",
  "recommended_tier": "CHEAP" | "MEDIUM" | "PREMIUM",
  "reason": "<one sentence>"
}}

Guidance:
- SIMPLE: one factual lookup from telemetry (a current value, a count, a status, an incident field). No reasoning. Example: "What is service X's memory usage right now?"
- MEDIUM: explaining or comparing using one or two signals - a single-service "why" question, a comparison, one correlation, a trend. Example: "Why is service X slow right now?"
- HARD: reserve for genuinely multi-step investigation ACROSS several services or signal types: full root-cause chains, event timelines, blast-radius assessments, remediation plans, architecture recommendations. Example: "Correlate CPU, latency and database data, identify the root cause and recommend fixes."
- Most ops queries are SIMPLE or MEDIUM. A MEDIUM-tier model explains single-service issues well; do NOT recommend PREMIUM for them.
- quality_requirement: CRITICAL/HIGH only when a wrong answer is costly (root-cause conclusions, remediation plans); otherwise MEDIUM or LOW.
- latency_requirement is HIGH only if the user explicitly asks for a quick/immediate answer; otherwise LOW or MEDIUM.
- The context provided to the assistant is about {context_tokens} tokens.

User query: {query}"""

# Minimum capability rank implied by each decision field (cheap=1, medium=2, premium=3)
COMPLEXITY_RANK = {"SIMPLE": 1, "MEDIUM": 2, "HARD": 3}
QUALITY_RANK = {"LOW": 1, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}
# latency_requirement -> max acceptable model max_latency_ms
LATENCY_CEILING = {"HIGH": 3000, "MEDIUM": 5000, "LOW": 10**9}


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def parse_decision(raw: str) -> dict:
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        raise ValueError(f"no JSON object in router output: {raw[:120]}")
    d = json.loads(m.group(0))
    for field in ("complexity", "recommended_tier"):
        if field not in d:
            raise ValueError(f"router output missing '{field}'")
    d["complexity"] = str(d["complexity"]).upper()
    d["recommended_tier"] = str(d["recommended_tier"]).upper()
    return d


def select_tier(decision: dict, settings: Settings, context_tokens: int) -> tuple:
    """Pick the cheapest tier satisfying the decision's requirements.

    Returns (tier_key, note). Constraints, in order of application:
      1. capability rank >= max(complexity, quality, reasoning, recommended_tier)
      2. model context window fits the prompt
      3. model max latency within the latency requirement ceiling
    If the latency constraint eliminates every candidate it is relaxed (quality
    beats speed for an ops assistant).
    """
    min_rank = max(
        COMPLEXITY_RANK.get(decision.get("complexity", "MEDIUM"), 2),
        QUALITY_RANK.get(str(decision.get("quality_requirement", "MEDIUM")).upper(), 1),
        2 if decision.get("reasoning_required") else 1,
        TIER_RANK.get(decision.get("recommended_tier", "").lower(), 1),
    )
    candidates = [
        t for t in settings.tiers_by_cost()
        if t.rank >= min_rank and t.max_context_tokens >= context_tokens + 800
    ]
    if not candidates:  # context too large for every capable tier -> biggest window
        big = max(settings.tiers.values(), key=lambda t: t.max_context_tokens)
        return big.key, "context size forced largest-window model"
    ceiling = LATENCY_CEILING.get(str(decision.get("latency_requirement", "LOW")).upper(), 10**9)
    fast = [t for t in candidates if t.max_latency_ms <= ceiling]
    chosen = (fast or candidates)[0]
    note = "cheapest model satisfying requirements"
    if not fast:
        note += " (latency constraint relaxed)"
    return chosen.key, note


async def route(query: str, context: str, settings: Settings, pool: ClientPool) -> dict:
    context_tokens = estimate_tokens(context)
    prompt = ROUTER_PROMPT.format(query=query, context_tokens=context_tokens)
    router = settings.router
    try:
        resp = await pool.get(router.provider).chat(
            model=router.name,
            messages=[{"role": "user", "content": prompt}],
            temperature=router.params.get("temperature", 0),
            max_tokens=router.params.get("max_tokens", 300),
        )
        decision = parse_decision(resp.content)
        router_cost = (resp.input_tokens / 1_000_000) * router.input_cost_per_1m + \
                      (resp.output_tokens / 1_000_000) * router.output_cost_per_1m
        tier_key, note = select_tier(decision, settings, context_tokens)
        return {
            "strategy": "intelligent",
            "complexity": decision.get("complexity"),
            "reasoning_required": decision.get("reasoning_required"),
            "context_requirement": decision.get("context_requirement"),
            "quality_requirement": decision.get("quality_requirement"),
            "latency_requirement": decision.get("latency_requirement"),
            "recommended_tier": decision.get("recommended_tier"),
            "selected_tier": tier_key,
            "selection_note": note,
            "reason": decision.get("reason", ""),
            "router_cost_usd": round(router_cost, 8),
            "router_latency_ms": resp.latency_ms,
            "fallback": False,
        }
    except (LLMError, ValueError, json.JSONDecodeError) as e:
        fb = rule_router.route(query)
        fb.update(strategy="intelligent", fallback=True,
                  reason=f"Router failed ({str(e)[:80]}); fell back to rule-based routing.")
        return fb
