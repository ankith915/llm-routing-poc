"""What-if simulator and the build/buy calculator.

Both are *models*, not measurements, and every response says so. The what-if
simulator's defaults are seeded from recorded traffic where it exists, so the
starting point is the client's own shape rather than a vendor's.
"""
import math
from dataclasses import dataclass

from backend.analytics.aggregate import _f, live
from backend.optimizer import pricing


def defaults_from(records: list, registry) -> dict:
    """Seed the simulator from real traffic when there is any."""
    rs = live(records)
    served = [r for r in rs if not r.get("cache_hit")]
    premium = registry.tier_default("premium")
    if not served:
        return {"requests_per_month": 1_000_000, "input_tokens": 2000, "output_tokens": 500,
                "cache_hit_rate": 20.0, "context_reduction_pct": 25.0, "batch_pct": 15.0,
                "mix": {"cheap": 60.0, "medium": 30.0, "premium": 10.0},
                "baseline_model": premium.id, "seeded_from": "none (illustrative defaults)"}
    n = len(rs)
    mix = {"cheap": 0.0, "medium": 0.0, "premium": 0.0}
    for r in served:
        t = r.get("tier")
        if t in mix:
            mix[t] += 1
    tot = sum(mix.values()) or 1
    inp = sum(int(_f(r, "input_tokens", 0)) for r in served) / max(1, len(served))
    base_inp = sum(int(_f(r, "baseline_input_tokens", 0)) for r in served) / max(1, len(served))
    out = sum(int(_f(r, "output_tokens", 0)) for r in served) / max(1, len(served))
    return {
        "requests_per_month": 1_000_000,
        "input_tokens": round(base_inp or inp),
        "output_tokens": round(out) or 250,
        "cache_hit_rate": round(100 * sum(1 for r in rs if r.get("cache_hit")) / n, 1),
        "context_reduction_pct": round(100 * (base_inp - inp) / base_inp, 1) if base_inp else 0.0,
        "batch_pct": round(100 * sum(1 for r in rs if r.get("execution_mode") in ("batch", "offline")) / n, 1),
        "mix": {k: round(100 * v / tot, 1) for k, v in mix.items()},
        "baseline_model": premium.id,
        "seeded_from": f"{n} recorded request(s)",
    }


def simulate(params: dict, registry) -> dict:
    """Model monthly spend for a baseline and an optimised configuration.

    The arithmetic is deliberately transparent and shown in the response so a
    client can check it: no hidden multipliers.
    """
    reqs = max(0, int(params.get("requests_per_month", 1_000_000)))
    inp = max(0, int(params.get("input_tokens", 2000)))
    out = max(0, int(params.get("output_tokens", 500)))
    cache = min(100.0, max(0.0, float(params.get("cache_hit_rate", 20.0)))) / 100
    ctx = min(95.0, max(0.0, float(params.get("context_reduction_pct", 25.0)))) / 100
    batch = min(100.0, max(0.0, float(params.get("batch_pct", 15.0)))) / 100
    mix = params.get("mix") or {"cheap": 60, "medium": 30, "premium": 10}
    total_mix = sum(max(0.0, float(v)) for v in mix.values()) or 1.0
    mix = {k: max(0.0, float(v)) / total_mix for k, v in mix.items()}
    base_model = registry.get(params.get("baseline_model") or registry.tier_default("premium").id)

    baseline_per_req = pricing.price(base_model, inp, out, registry=registry).total_usd
    baseline = reqs * baseline_per_req

    served = reqs * (1 - cache)
    inp_opt = round(inp * (1 - ctx))
    lines, optimised = [], 0.0
    for tier, share in mix.items():
        try:
            m = registry.tier_default(tier)
        except StopIteration:
            continue
        n_tier = served * share
        n_batch = n_tier * batch
        n_sync = n_tier - n_batch
        c_sync = pricing.price(m, inp_opt, out, registry=registry).total_usd
        c_batch = pricing.price(m, inp_opt, out, execution_mode="batch", registry=registry).total_usd
        cost = n_sync * c_sync + n_batch * c_batch
        optimised += cost
        lines.append({"tier": tier, "model": m.id, "share_pct": round(100 * share, 1),
                      "requests": round(n_tier), "batch_requests": round(n_batch),
                      "cost_per_request_usd": round(c_sync, 8),
                      "batch_cost_per_request_usd": round(c_batch, 8),
                      "monthly_usd": round(cost, 2),
                      "batch_discount": registry.provider(m.provider).batch_discount})
    saving = baseline - optimised
    # Contribution of each lever, computed by turning it off one at a time.
    def _spend(cache_v=cache, ctx_v=ctx, batch_v=batch, mix_v=mix):
        s = reqs * (1 - cache_v)
        i = round(inp * (1 - ctx_v))
        tot = 0.0
        for tier, share in mix_v.items():
            try:
                m = registry.tier_default(tier)
            except StopIteration:
                continue
            nt = s * share
            nb = nt * batch_v
            tot += (nt - nb) * pricing.price(m, i, out, registry=registry).total_usd
            tot += nb * pricing.price(m, i, out, execution_mode="batch", registry=registry).total_usd
        return tot
    all_premium = {"cheap": 0.0, "medium": 0.0, "premium": 1.0}
    levers = [
        {"lever": "Cache", "usd": round(_spend(cache_v=0.0) - optimised, 2)},
        {"lever": "Context reduction", "usd": round(_spend(ctx_v=0.0) - optimised, 2)},
        {"lever": "Model routing", "usd": round(_spend(mix_v=all_premium) - optimised, 2)},
        {"lever": "Batch execution", "usd": round(_spend(batch_v=0.0) - optimised, 2)},
    ]
    return {
        "basis": "modeled",
        "note": "A model of monthly spend under the stated assumptions, not a measurement. Prices come "
                "from the versioned registry; real savings depend on the traffic mix and quality "
                "requirements of the actual workload.",
        "price_registry_version": registry.version,
        "inputs": {"requests_per_month": reqs, "input_tokens": inp, "output_tokens": out,
                   "cache_hit_rate_pct": round(100 * cache, 1),
                   "context_reduction_pct": round(100 * ctx, 1), "batch_pct": round(100 * batch, 1),
                   "mix_pct": {k: round(100 * v, 1) for k, v in mix.items()},
                   "baseline_model": base_model.id},
        "baseline": {"model": base_model.id, "cost_per_request_usd": round(baseline_per_req, 8),
                     "monthly_usd": round(baseline, 2), "annual_usd": round(baseline * 12, 2)},
        "optimized": {"monthly_usd": round(optimised, 2), "annual_usd": round(optimised * 12, 2),
                      "cost_per_request_usd": round(optimised / reqs, 8) if reqs else 0.0,
                      "cached_requests": round(reqs * cache), "served_requests": round(served),
                      "input_tokens_per_request": inp_opt, "lines": lines},
        "savings": {"monthly_usd": round(saving, 2), "annual_usd": round(saving * 12, 2),
                    "pct": round(100 * saving / baseline, 1) if baseline else 0.0},
        "lever_contributions": sorted(levers, key=lambda l: -l["usd"]),
    }


def build_vs_buy(params: dict, registry) -> dict:
    """Fully loaded self-hosting cost against the equivalent API spend.

    Self-hosting is only cheaper past a real utilisation threshold, and the
    calculator is built to show that rather than to argue for it.
    """
    reqs_day = max(0.0, float(params.get("requests_per_day", 100_000)))
    inp = max(0, int(params.get("input_tokens", 2000)))
    out = max(0, int(params.get("output_tokens", 500)))
    gpu_hourly = max(0.0, float(params.get("gpu_hourly_usd", 2.99)))
    gpus = max(1, int(params.get("gpu_count", 1)))
    util = min(100.0, max(1.0, float(params.get("utilization_pct", 40.0)))) / 100
    throughput = max(1.0, float(params.get("tokens_per_second_per_gpu", 2500)))
    eng_monthly = max(0.0, float(params.get("engineering_monthly_usd", 12_000)))
    ops_multiplier = max(1.0, float(params.get("ops_multiplier", 1.0)))
    storage = max(0.0, float(params.get("storage_monthly_usd", 200)))
    egress = max(0.0, float(params.get("egress_monthly_usd", 150)))
    observability = max(0.0, float(params.get("observability_monthly_usd", 300)))
    redundancy = max(1.0, float(params.get("redundancy_factor", 1.5)))
    api_model_id = params.get("api_model") or registry.tier_default("medium").id
    api_model = registry.get(api_model_id)

    tokens_day = reqs_day * (inp + out)
    monthly_reqs = reqs_day * 30.4
    api_monthly = monthly_reqs * pricing.price(api_model, inp, out, registry=registry).total_usd

    gpu_monthly = gpu_hourly * 24 * 30.4 * gpus * redundancy
    infra = gpu_monthly + storage + egress + observability
    people = eng_monthly * ops_multiplier
    self_monthly = infra + people
    capacity_tokens_day = throughput * 86400 * gpus * util
    capacity_ok = capacity_tokens_day >= tokens_day
    needed_gpus = math.ceil(tokens_day / (throughput * 86400 * util)) if throughput and util else None
    per_million_self = (self_monthly / (tokens_day * 30.4 / 1e6)) if tokens_day else None
    per_million_api = pricing.price(api_model, 1_000_000, 0, registry=registry).total_usd + \
        pricing.price(api_model, 0, 1_000_000, registry=registry).total_usd * (out / max(1, inp + out))

    # Break-even: daily tokens at which self-hosting matches the API bill.
    api_per_token = (api_monthly / (tokens_day * 30.4)) if tokens_day else 0.0
    breakeven_tokens_day = (self_monthly / 30.4 / api_per_token) if api_per_token else None
    breakeven_requests_day = (breakeven_tokens_day / (inp + out)) if breakeven_tokens_day and (inp + out) else None

    verdict = "api"
    if capacity_ok and self_monthly < api_monthly:
        verdict = "self_host"
    elif not capacity_ok:
        verdict = "api_capacity"
    return {
        "basis": "modeled",
        "note": "Fully loaded comparison. Self-hosting is only cheaper past a real utilisation "
                "threshold; engineering and on-call time usually dominate the GPU bill at POC volume.",
        "inputs": {**params, "api_model": api_model.id},
        "api": {"model": api_model.id, "monthly_usd": round(api_monthly, 2),
                "annual_usd": round(api_monthly * 12, 2),
                "per_million_tokens_usd": round(per_million_api, 4)},
        "self_hosted": {
            "monthly_usd": round(self_monthly, 2), "annual_usd": round(self_monthly * 12, 2),
            "gpu_monthly_usd": round(gpu_monthly, 2), "people_monthly_usd": round(people, 2),
            "other_monthly_usd": round(storage + egress + observability, 2),
            "people_share_pct": round(100 * people / self_monthly, 1) if self_monthly else 0.0,
            "per_million_tokens_usd": round(per_million_self, 4) if per_million_self else None,
            "capacity_tokens_per_day": round(capacity_tokens_day),
            "required_tokens_per_day": round(tokens_day),
            "capacity_sufficient": capacity_ok,
            "gpus_required": needed_gpus,
        },
        "breakeven": {"tokens_per_day": round(breakeven_tokens_day) if breakeven_tokens_day else None,
                      "requests_per_day": round(breakeven_requests_day) if breakeven_requests_day else None,
                      "current_tokens_per_day": round(tokens_day)},
        "verdict": verdict,
        "verdict_text": {
            "api": f"Stay on the API. Self-hosting would cost ${self_monthly:,.0f}/month against "
                   f"${api_monthly:,.0f}/month, and {round(100 * people / self_monthly) if self_monthly else 0}% "
                   f"of that is people, not GPUs.",
            "api_capacity": f"Stay on the API. The configured fleet serves "
                            f"{round(capacity_tokens_day):,} tokens/day at {round(100 * util)}% utilisation "
                            f"but the workload needs {round(tokens_day):,}; "
                            f"{needed_gpus} GPU(s) would be required.",
            "self_host": f"Self-hosting is cheaper at this volume: ${self_monthly:,.0f}/month against "
                         f"${api_monthly:,.0f}/month. Confirm the utilisation assumption "
                         f"({round(100 * util)}%) holds at the traffic's real peak-to-trough shape.",
        }[verdict],
    }
