"""Query orchestration: route -> answer -> cost -> (optional) evaluate."""
import datetime

from backend import storage, telemetry
from backend.config.loader import Settings
from backend.evaluation import evaluator
from backend.evaluation.metrics import calculate_cost
from backend.compression import compress_context
from backend.llm.client import ClientPool, LLMError
from backend.router import intelligent_router, rule_router

async def decide(strategy: str, query: str, context: str,
                 settings: Settings, pool: ClientPool) -> dict:
    if strategy == "none":
        return {"strategy": "none", "complexity": None, "selected_tier": "premium",
                "recommended_tier": "PREMIUM", "reason": "Baseline: every query uses the premium model.",
                "router_cost_usd": 0.0, "router_latency_ms": 0}
    if strategy == "rule":
        return rule_router.route(query)
    if strategy == "intelligent":
        return await intelligent_router.route(query, context, settings, pool)
    raise ValueError(f"unknown strategy '{strategy}' (use none|rule|intelligent)")


def fallback_chain(settings: Settings, tier) -> list:
    """Tiers to try for one answer, best-first.

    A flaky provider call must not drop a cell from the comparison, so we try
    the routed tier, then escalate to more capable tiers (a costlier answer is
    better than a missing one), then descend if nothing above is left.
    """
    others = [t for t in settings.tiers.values() if t.key != tier.key]
    higher = sorted((t for t in others if t.rank > tier.rank), key=lambda t: t.rank)
    lower = sorted((t for t in others if t.rank < tier.rank), key=lambda t: -t.rank)
    return [tier, *higher, *lower]


async def answer_with_fallback(query: str, context: str, settings: Settings,
                               pool: ClientPool, tier):
    """Answer at `tier`, falling back down the chain. Returns (tier, resp, used_fallback).

    Raises LLMError only if every configured tier fails.
    """
    messages = [
        {"role": "system", "content": telemetry.SYSTEM_PROMPT},
        {"role": "user", "content": f"Telemetry:\n{context}\n\nQuestion: {query}"},
    ]
    errors, tried = [], 0
    for candidate in fallback_chain(settings, tier):
        if not pool.has(candidate.provider):
            continue                      # provider has no key configured
        try:
            resp = await pool.get(candidate.provider).chat(
                model=candidate.name,
                messages=messages,
                temperature=settings.answer_params.get("temperature", 0.2),
                max_tokens=settings.answer_params.get("max_tokens", 900),
                extra_params=candidate.request_params,
            )
            # Belt and braces: the client rejects empty completions too, but a
            # blank answer must never reach the judge, whatever produced it.
            if not resp.content.strip():
                raise LLMError(f"{candidate.name} returned an empty answer")
            # Count only tiers actually called, so an unconfigured provider
            # earlier in the chain is not mistaken for a fallback.
            return candidate, resp, tried > 0
        except LLMError as e:
            errors.append(f"{candidate.key}({candidate.name}): {e}")
        tried += 1
    raise LLMError("all tiers failed - " + "; ".join(errors))


async def run_query(query: str, strategy: str, settings: Settings, pool: ClientPool,
                    evaluate: bool = False, test_query: dict | None = None,
                    compression: str = "off") -> dict:
    context = telemetry.build_context(query)
    # Compress before routing so the router and the answer call see the same
    # context - exactly what a deployment with compression in front would do.
    context, cstats = compress_context(context, query, compression)
    routing = await decide(strategy, query, context, settings, pool)
    tier = settings.tier(routing["selected_tier"])

    routed_tier = tier
    tier, resp, used_fallback = await answer_with_fallback(
        query, context, settings, pool, tier)
    # Cost is attributed to the tier that actually answered, not the one the
    # router picked - otherwise a fallback would understate the real spend.
    answer_cost = calculate_cost(tier, resp.input_tokens, resp.output_tokens)
    total_cost = answer_cost + routing.get("router_cost_usd", 0.0)
    total_latency = resp.latency_ms + routing.get("router_latency_ms", 0)

    record = {
        "request_id": await storage.next_request_id(),
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "query_id": test_query["id"] if test_query else None,
        "query": query,
        "strategy": strategy,
        "tier": tier.key,
        "model": tier.name,
        "tier_provider": tier.provider,
        "pricing_note": tier.pricing_note,
        "fallback_used": used_fallback,
        "routed_tier": routed_tier.key,
        "compression": cstats["mode"],
        "context_tokens_before": cstats["tokens_before"],
        "context_tokens_after": cstats["tokens_after"],
        "context_saved_pct": cstats["saved_pct"],
        "compression_error": cstats["error"],
        "model_used": resp.model_used,
        "provider": resp.provider,
        "expected_complexity": test_query["complexity"] if test_query else None,
        "routing": routing,
        "answer": resp.content,
        "input_tokens": resp.input_tokens,
        "output_tokens": resp.output_tokens,
        "answer_latency_ms": resp.latency_ms,
        "latency_ms": total_latency,
        "answer_cost_usd": round(answer_cost, 8),
        "cost_usd": round(total_cost, 8),
        "quality_score": None,
        "quality_rationale": None,
    }

    if test_query is None:
        test_query = next((t for t in telemetry.load_test_queries()
                           if t["query"].strip().lower() == query.strip().lower()), None)
        if test_query:
            record["query_id"] = test_query["id"]
            record["expected_complexity"] = test_query["complexity"]
    if evaluate and test_query:
        record.update(await evaluator.evaluate(
            query, test_query["expected_answer"], test_query["criteria"],
            resp.content, settings, pool))

    await storage.append(record)
    return record
