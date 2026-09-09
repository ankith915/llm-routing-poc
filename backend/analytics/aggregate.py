"""Aggregations over the cost ledger. Every number here comes from stored
request records - nothing is invented, and anything modelled says so.
"""
import datetime
import statistics
from collections import defaultdict

from backend.evaluation import metrics
from backend.optimizer import tasks
from backend.optimizer.ledger import LAYER_LABELS

WASTE_LAYERS = ("overhead", "escalation", "fallback")


def live(records: list) -> list:
    """Records that can be reasoned about financially.

    Excludes shadow traffic (it was served by the incumbent), rejected requests
    (nothing was spent) and records written before the cost ledger existed. A
    legacy record has no baseline and no attribution, so including it would
    quietly understate savings; `legacy_count` reports how many were set aside
    rather than dropping them silently.
    """
    return [r for r in records
            if r.get("mode") != "shadow" and not r.get("rejected") and r.get("engine_version")]


def legacy_count(records: list) -> int:
    return sum(1 for r in records if not r.get("engine_version") and not r.get("rejected"))


def _f(r, key, default=0.0):
    v = r.get(key)
    return default if v is None else v


def scope(records: list, tenant_id=None, application_id=None, environment=None,
          strategy=None, task_type=None, since=None) -> list:
    out = records
    for key, val in (("tenant_id", tenant_id), ("application_id", application_id),
                     ("environment", environment), ("strategy", strategy), ("task_type", task_type)):
        if val:
            out = [r for r in out if r.get(key) == val]
    if since:
        out = [r for r in out if (r.get("timestamp") or "") >= since]
    return out


# --------------------------------------------------------------------- overview

def overview(records: list) -> dict:
    rs = live(records)
    costs = [_f(r, "cost_usd") for r in rs]
    base = [_f(r, "baseline_cost_usd") for r in rs]
    lats = [_f(r, "latency_ms") for r in rs]
    quals = [r["quality_score"] for r in rs if r.get("quality_score") is not None]
    gated = [r for r in rs if r.get("quality_gate_passed") is not None]
    passed = [r for r in gated if r["quality_gate_passed"]]
    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    month = today[:7]
    spend_today = sum(_f(r, "cost_usd") for r in rs if (r.get("timestamp") or "").startswith(today))
    spend_month = sum(_f(r, "cost_usd") for r in rs if (r.get("timestamp") or "").startswith(month))
    total_cost, total_base = sum(costs), sum(base)
    return {
        "requests": len(rs),
        "spend_usd": round(total_cost, 6),
        "spend_today_usd": round(spend_today, 6),
        "spend_month_usd": round(spend_month, 6),
        "baseline_spend_usd": round(total_base, 6),
        "savings_usd": round(total_base - total_cost, 6),
        "savings_pct": round(100 * (total_base - total_cost) / total_base, 1) if total_base else 0.0,
        "avg_cost_usd": round(statistics.fmean(costs), 8) if costs else 0.0,
        "cost_per_successful_task_usd": (round(total_cost / len(passed), 8) if passed else None),
        "successful_tasks": len(passed),
        "quality_avg": round(statistics.fmean(quals), 3) if quals else None,
        "quality_pass_rate": round(100 * len(passed) / len(gated), 1) if gated else None,
        "quality_evaluated": len(quals),
        "p50_latency_ms": round(metrics.percentile(lats, 50)),
        "p95_latency_ms": round(metrics.percentile(lats, 95)),
        "cache_hit_rate": round(100 * sum(1 for r in rs if r.get("cache_hit")) / len(rs), 1) if rs else 0.0,
        "cache_hits": sum(1 for r in rs if r.get("cache_hit")),
        "frontier_pct": round(100 * sum(1 for r in rs if r.get("tier") == "premium") / len(rs), 1) if rs else 0.0,
        "escalation_rate": round(100 * sum(1 for r in rs if r.get("escalation_count")) / len(rs), 1) if rs else 0.0,
        "fallback_rate": round(100 * sum(1 for r in rs if r.get("fallback_used")) / len(rs), 1) if rs else 0.0,
        "input_tokens": sum(int(_f(r, "input_tokens", 0)) for r in rs),
        "output_tokens": sum(int(_f(r, "output_tokens", 0)) for r in rs),
        "tokens_removed": sum(max(0, int(_f(r, "baseline_input_tokens", 0)) - int(_f(r, "input_tokens", 0)))
                              for r in rs),
        "shadow_requests": sum(1 for r in records if r.get("mode") == "shadow"),
        "rejected_requests": sum(1 for r in records if r.get("rejected")),
        "legacy_requests": legacy_count(records),
        "baseline_source_mix": _count(rs, "baseline_source"),
    }


def _count(records, key):
    out = defaultdict(int)
    for r in records:
        out[r.get(key) or "unknown"] += 1
    return dict(out)


# -------------------------------------------------------------------- waterfall

def waterfall(records: list) -> dict:
    """Baseline spend, one bar per optimisation layer, final spend.

    Bars are summed from the per-request ledger attribution, so the waterfall is
    an aggregation of measured rows, not a separate calculation that could drift.
    """
    rs = live(records)
    bars, basis, counts = defaultdict(float), defaultdict(set), defaultdict(int)
    for r in rs:
        for a in (r.get("ledger") or {}).get("attribution", []):
            bars[a["layer"]] += a["usd"]
            basis[a["layer"]].add(a["basis"])
            counts[a["layer"]] += 1
    baseline = sum(_f(r, "baseline_cost_usd") for r in rs)
    final = sum(_f(r, "cost_usd") for r in rs)
    order = ["exact_cache", "semantic_cache", "context", "routing", "reasoning", "execution_mode",
             "provider_cache", "overhead", "escalation", "fallback"]
    steps, running = [], baseline
    for layer in order:
        if layer not in bars or abs(bars[layer]) < 1e-9:
            continue
        delta = bars[layer]
        steps.append({"layer": layer, "label": LAYER_LABELS.get(layer, layer.replace("_", " ").title()),
                      "usd": round(delta, 6), "from_usd": round(running, 6),
                      "to_usd": round(running - delta, 6), "requests": counts[layer],
                      "basis": sorted(basis[layer]),
                      "pct_of_baseline": round(100 * delta / baseline, 2) if baseline else 0.0})
        running -= delta
    return {
        "baseline_usd": round(baseline, 6), "final_usd": round(final, 6),
        "savings_usd": round(baseline - final, 6),
        "savings_pct": round(100 * (baseline - final) / baseline, 1) if baseline else 0.0,
        "steps": steps, "requests": len(rs),
        "reconciles": abs(running - final) < 1e-6,
        "residual_usd": round(running - final, 9),
        "baseline_source_mix": _count(rs, "baseline_source"),
    }


# ------------------------------------------------------------------------ routing

def routing(records: list) -> dict:
    rs = live(records)
    by_model, by_provider, by_task, by_rung = defaultdict(list), defaultdict(int), defaultdict(list), defaultdict(int)
    for r in rs:
        model = r.get("model") or "unknown"
        by_model[model].append(r)
        by_provider[r.get("provider") or "unknown"] += 1
        if r.get("task_type"):
            by_task[r["task_type"]].append(r)
        rung = ((r.get("classification") or {}).get("rung")) or "n/a"
        by_rung[rung] += 1
    n = len(rs) or 1
    return {
        "by_model": [{"model": m, "requests": len(v), "pct": round(100 * len(v) / n, 1),
                      "cost_usd": round(sum(_f(x, "cost_usd") for x in v), 6),
                      "avg_quality": _avg_q(v), "tier": next((x.get("tier") for x in v if x.get("tier")), None)}
                     for m, v in sorted(by_model.items(), key=lambda kv: -len(kv[1]))],
        "by_provider": dict(by_provider),
        "by_task": [{"task_type": t, "label": tasks.get(t).label, "requests": len(v),
                     "cost_usd": round(sum(_f(x, "cost_usd") for x in v), 6),
                     "avg_quality": _avg_q(v),
                     "frontier_pct": round(100 * sum(1 for x in v if x.get("tier") == "premium") / len(v), 1),
                     "models": _count(v, "model")}
                    for t, v in sorted(by_task.items(), key=lambda kv: -len(kv[1]))],
        "classifier_rungs": dict(by_rung),
        "llm_router_cost_usd": round(sum(_f(r, "router_cost_usd") for r in rs), 6),
        "embedding_cost_usd": round(sum(_f(r, "embedding_cost_usd") for r in rs), 6),
        "router_overhead_pct_of_savings": _overhead_pct(rs),
    }


def _overhead_pct(rs):
    savings = sum(_f(r, "savings_usd") for r in rs)
    overhead = sum(_f(r, "router_cost_usd") + _f(r, "embedding_cost_usd") for r in rs)
    return round(100 * overhead / savings, 2) if savings > 0 else None


def _avg_q(rows):
    q = [r["quality_score"] for r in rows if r.get("quality_score") is not None]
    return round(statistics.fmean(q), 2) if q else None


# -------------------------------------------------------------------------- cache

def cache_analytics(records: list) -> dict:
    rs = live(records)
    exact = [r for r in rs if r.get("cache_kind") == "exact"]
    sem = [r for r in rs if r.get("cache_kind") == "semantic"]
    considered = [r for r in rs if any(s["stage"] == "exact_cache" and s["status"] in ("hit", "miss")
                                       for s in (r.get("trace") or []))]
    avoided = sum(_f(r, "baseline_cost_usd") for r in exact + sem)
    rejected = []
    for r in rs:
        for s in (r.get("trace") or []):
            if s["stage"] == "semantic_cache":
                for rej in (s["detail"] or {}).get("rejected", [])[:3]:
                    rejected.append({**rej, "request_id": r.get("request_id"), "query": r.get("query")})
    rejected.sort(key=lambda x: -(x.get("similarity") or 0))
    return {
        "requests_considered": len(considered),
        "exact_hits": len(exact), "semantic_hits": len(sem),
        "exact_hit_rate": round(100 * len(exact) / len(considered), 1) if considered else 0.0,
        "semantic_hit_rate": round(100 * len(sem) / len(considered), 1) if considered else 0.0,
        # Hit rate and money saved are reported separately on purpose: a high hit
        # rate on cheap requests saves little, and conflating them hides that.
        "cost_avoided_usd": round(avoided, 6),
        "embedding_cost_usd": round(sum(_f(r, "embedding_cost_usd") for r in rs), 6),
        "net_cache_benefit_usd": round(avoided - sum(_f(r, "embedding_cost_usd") for r in rs), 6),
        "latency_avoided_ms": sum(int(_f(r, "answer_latency_ms", 0)) for r in exact + sem),
        "provider_cached_input_tokens": sum(int(_f(r, "cached_input_tokens", 0)) for r in rs),
        "near_misses": rejected[:10],
    }


# ------------------------------------------------------------------------ quality

def quality(records: list) -> dict:
    rs = live(records)
    gated = [r for r in rs if r.get("quality_gate_passed") is not None]
    validators = defaultdict(lambda: {"pass": 0, "fail": 0})
    for r in rs:
        for v in ((r.get("quality") or {}).get("validators") or []):
            validators[v["kind"]]["pass" if v["passed"] else "fail"] += 1
    esc = [r for r in rs if r.get("escalation_count")]
    return {
        "evaluated": sum(1 for r in rs if r.get("quality_score") is not None),
        "gate_pass_rate": round(100 * sum(1 for r in gated if r["quality_gate_passed"]) / len(gated), 1)
                          if gated else None,
        "gate_failures": [{"request_id": r["request_id"], "query": r.get("query"),
                           "model": r.get("model"), "reason": r.get("quality_reason"),
                           "escalated_to": r.get("model") if r.get("escalated_from") else None,
                           "escalated_from": r.get("escalated_from")}
                          for r in rs if r.get("quality_gate_passed") is False][:20],
        "escalations": [{"request_id": r["request_id"], "query": r.get("query"),
                         "from": r.get("escalated_from"), "to": r.get("model"),
                         "extra_cost_usd": round(_f(r, "escalation_cost_usd"), 8),
                         "final_score": r.get("quality_score")} for r in esc][:20],
        "validators": {k: v for k, v in validators.items()},
        "by_model": metrics.quality_breakdowns(rs)["by_model"],
        "by_task": metrics.quality_breakdowns(rs)["by_task"],
        "by_complexity": metrics.quality_breakdowns(rs)["by_complexity"],
        "score_distribution": metrics.quality_breakdowns(rs)["score_distribution"],
        "judge_cost_note": "Judge calls are offline evaluation, not serving cost, and are excluded "
                           "from cost_usd. Validator checks are free and deterministic.",
    }


def unit_economics(records: list) -> dict:
    rs = live(records)
    out = []
    for key in ("application_id", "task_type", "feature_id", "tenant_id"):
        groups = defaultdict(list)
        for r in rs:
            groups[r.get(key) or "unknown"].append(r)
        rows = []
        for name, v in sorted(groups.items(), key=lambda kv: -sum(_f(x, "cost_usd") for x in kv[1])):
            ok = [x for x in v if x.get("quality_gate_passed") is not False]
            cost = sum(_f(x, "cost_usd") for x in v)
            base = sum(_f(x, "baseline_cost_usd") for x in v)
            rows.append({"name": name, "requests": len(v), "cost_usd": round(cost, 6),
                         "baseline_usd": round(base, 6),
                         "savings_pct": round(100 * (base - cost) / base, 1) if base else 0.0,
                         "cost_per_request_usd": round(cost / len(v), 8),
                         "cost_per_successful_task_usd": round(cost / len(ok), 8) if ok else None,
                         "success_rate": round(100 * len(ok) / len(v), 1),
                         "avg_quality": _avg_q(v)})
        out.append({"dimension": key, "rows": rows})
    return {"dimensions": out}
