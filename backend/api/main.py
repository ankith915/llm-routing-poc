"""FastAPI application: the optimizer control plane and its dashboard."""
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from backend import compression, db, demo, experiment, storage, telemetry
from backend.analytics import aggregate, simulator, waste
from backend.api.auth import Principal, admin, auth_enabled, principal
from backend.config.loader import load_settings
from backend.evaluation import evaluator
from backend.evaluation.metrics import (STRATEGIES, baseline_records, comparison,
                                        compression_breakdown, overview, quality_breakdowns)
from backend.llm.client import ClientPool, LLMClient, LLMError
from backend.optimizer import cache as cache_mod
from backend.optimizer import classifier as classifier_mod
from backend.optimizer import budget as budget_mod
from backend.optimizer import tasks as tasks_mod
from backend.optimizer.envelope import RequestEnvelope
from backend.optimizer.health import CHAOS_MODES, get_health
from backend.optimizer.policy import get_policies
from backend.optimizer.registry import get_registry
from backend import pipeline

FRONTEND_DIR = Path(__file__).resolve().parent.parent.parent / "frontend"

settings = load_settings()
pool: ClientPool = ClientPool({})


@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool
    await storage.init()
    health = get_health()
    pool = ClientPool.from_settings(settings, chaos=health.chaos_for, on_result=health.record)
    yield
    await pool.aclose()
    await db.close()


app = FastAPI(title="AI Inference Cost Optimizer", version="2.0",
              description="A provider-neutral control plane that picks the cheapest safe path "
                          "to the required business outcome, and explains every decision.",
              lifespan=lifespan)


def require_pool() -> ClientPool:
    if not pool.providers:
        raise HTTPException(503, "No API keys configured. Copy .env.example to .env and set "
                                 "GROQ_API_KEY and OPENAI_API_KEY.")
    return pool


async def records_for(p: Principal, **filters) -> list:
    recs = await storage.all_records()
    if not p.admin:
        recs = [r for r in recs if r.get("tenant_id") in (p.tenant_id, None)]
    if filters:
        recs = aggregate.scope(recs, **filters)
    return recs


# ============================================================ optimizer requests

class QueryRequest(BaseModel):
    query: str
    strategy: str = "optimized"
    mode: str = "live"
    evaluate: bool = False
    tenant_id: str | None = None
    application_id: str | None = None
    sla_class: str | None = None
    sensitivity_class: str | None = None
    quality_target: float | None = None
    max_cost_usd: float | None = None
    task_type: str | None = None
    session_id: str | None = None
    compression: str | None = None


@app.post("/api/query")
async def api_query(req: QueryRequest, p: Principal = Depends(principal)):
    tenant = req.tenant_id if p.admin else p.tenant_id
    application = req.application_id or (p.application_id if not p.admin else "ops-assistant")
    if not p.may_use(req.application_id):
        raise HTTPException(403, f"this key may not act for application '{req.application_id}'")
    c = require_pool()
    try:
        env = RequestEnvelope(
            query=req.query, strategy=req.strategy, mode=req.mode, evaluate=req.evaluate,
            tenant_id=tenant or "acme", application_id=application, sla_class=req.sla_class,
            sensitivity_class=req.sensitivity_class, quality_target=req.quality_target,
            max_cost_usd=req.max_cost_usd, task_type_hint=req.task_type, session_id=req.session_id,
            compression=req.compression)
    except ValueError as e:
        raise HTTPException(400, str(e))
    try:
        return await pipeline.run_envelope(env, settings, c)
    except LLMError as e:
        raise HTTPException(502, str(e))


@app.post("/v1/chat/completions")
async def openai_chat_completions(request: Request, p: Principal = Depends(principal)):
    """OpenAI-compatible entry point.

    An application changes its base URL and keeps its code. The response is a
    standard chat completion with an extra `optimizer` block carrying the
    decision, the cost and the trace id.
    """
    body = await request.json()
    if not body.get("messages"):
        raise HTTPException(400, "messages is required")
    opt = body.get("optimizer") or {}
    if not p.may_use(opt.get("application_id")):
        raise HTTPException(403, "this key may not act for that application")
    try:
        env = RequestEnvelope.from_openai(
            body, tenant_id=(opt.get("tenant_id") if p.admin else p.tenant_id) or p.tenant_id,
            application_id=opt.get("application_id") or p.application_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    c = require_pool()
    try:
        r = await pipeline.run_envelope(env, settings, c)
    except LLMError as e:
        raise HTTPException(502, str(e))
    if r.get("rejected"):
        raise HTTPException(403, r.get("rejection_reason") or "rejected by policy")
    return JSONResponse({
        "id": f"chatcmpl-{r['request_id']}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": r.get("model_used") or r.get("model"),
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": r.get("answer", "")}}],
        "usage": {"prompt_tokens": r.get("input_tokens", 0),
                  "completion_tokens": r.get("output_tokens", 0),
                  "total_tokens": (r.get("input_tokens", 0) or 0) + (r.get("output_tokens", 0) or 0),
                  "prompt_tokens_details": {"cached_tokens": r.get("cached_input_tokens", 0)}},
        "optimizer": {
            "request_id": r["request_id"], "trace_id": r.get("trace_id"),
            "requested_model": (r.get("metadata") or {}).get("requested_model") or body.get("model"),
            "selected_model": r.get("model"), "provider": r.get("provider"),
            "task_type": r.get("task_type"), "difficulty": r.get("difficulty"),
            "reason": (r.get("routing") or {}).get("reason"),
            "cache": r.get("cache_kind"), "execution_mode": r.get("execution_mode"),
            "reasoning_level": r.get("reasoning_level"),
            "cost_usd": r.get("cost_usd"), "baseline_cost_usd": r.get("baseline_cost_usd"),
            "baseline_source": r.get("baseline_source"), "savings_pct": r.get("savings_pct"),
            "quality_score": r.get("quality_score"), "quality_gate_passed": r.get("quality_gate_passed"),
            "escalated_from": r.get("escalated_from"), "fallback_used": r.get("fallback_used"),
            "policy_version": r.get("policy_version"),
            "price_registry_version": r.get("price_registry_version"),
            "trace_url": f"/api/requests/{r['request_id']}",
        },
    }, headers={"x-optimizer-request-id": r["request_id"],
                "x-optimizer-model": str(r.get("model")),
                "x-optimizer-cost-usd": str(r.get("cost_usd"))})


@app.get("/v1/models")
async def openai_models(p: Principal = Depends(principal)):
    reg = get_registry()
    return {"object": "list", "data": [
        {"id": m.id, "object": "model", "owned_by": m.provider, "created": 0}
        for m in reg.candidates()]}


# ==================================================================== requests

@app.get("/api/requests")
async def api_requests(strategy: str | None = None, tenant_id: str | None = None,
                       application_id: str | None = None, task_type: str | None = None,
                       mode: str | None = None, limit: int = 200,
                       p: Principal = Depends(principal)):
    recs = await records_for(p, strategy=strategy, tenant_id=tenant_id,
                             application_id=application_id, task_type=task_type)
    if mode:
        recs = [r for r in recs if (r.get("mode") or "live") == mode]
    slim = [{k: v for k, v in r.items() if k not in ("trace", "answer", "ledger", "routing",
                                                     "context", "quality", "classification", "plan")}
            for r in recs[-limit:][::-1]]
    return {"count": len(recs), "requests": slim}


@app.get("/api/requests/{request_id}")
async def api_request_detail(request_id: str, p: Principal = Depends(principal)):
    r = await storage.get(request_id)
    if not r or (not p.admin and r.get("tenant_id") != p.tenant_id):
        raise HTTPException(404, f"request {request_id} not found")
    return r


# =================================================================== analytics

@app.get("/api/analytics/overview")
async def api_overview(tenant_id: str | None = None, application_id: str | None = None,
                       p: Principal = Depends(principal)):
    recs = await records_for(p, tenant_id=tenant_id, application_id=application_id)
    policies = get_policies()
    budgets = []
    seen = set()
    for r in aggregate.live(recs):
        key = (r.get("tenant_id"), r.get("application_id"))
        if key in seen or None in key:
            continue
        seen.add(key)
        budgets += [b.public() for b in await budget_mod.status(policies, key[0], key[1])]
    return {**aggregate.overview(recs), "budgets": budgets,
            "strategies": comparison(baseline_records(recs))["strategies"]}


@app.get("/api/analytics/waterfall")
async def api_waterfall(tenant_id: str | None = None, application_id: str | None = None,
                        strategy: str | None = None, p: Principal = Depends(principal)):
    recs = await records_for(p, tenant_id=tenant_id, application_id=application_id, strategy=strategy)
    return aggregate.waterfall(recs)


@app.get("/api/analytics/routing")
async def api_routing(p: Principal = Depends(principal)):
    return aggregate.routing(await records_for(p))


@app.get("/api/analytics/cache")
async def api_cache_analytics(p: Principal = Depends(principal)):
    recs = await records_for(p)
    return {**aggregate.cache_analytics(recs),
            "entries": await cache_mod.stats(None if p.admin else p.tenant_id),
            "separation": cache_mod.measure_separation()}


@app.get("/api/analytics/quality")
async def api_quality(p: Principal = Depends(principal)):
    return aggregate.quality(await records_for(p))


@app.get("/api/analytics/unit-economics")
async def api_unit_economics(p: Principal = Depends(principal)):
    return aggregate.unit_economics(await records_for(p))


@app.get("/api/analytics/waste")
async def api_waste(p: Principal = Depends(principal)):
    return waste.analyse(await records_for(p), get_registry(), get_policies())


@app.get("/api/analytics/recommendations")
async def api_recommendations(p: Principal = Depends(principal)):
    if not get_policies().flags()[0].get("ENABLE_RECOMMENDATIONS", True):
        return {"recommendations": [], "note": "Recommendations are disabled by feature flag."}
    return waste.recommend(await records_for(p), get_registry(), get_policies())


@app.get("/api/analytics/shadow")
async def api_shadow_report(p: Principal = Depends(principal)):
    recs = [r for r in await records_for(p) if r.get("mode") == "shadow" and r.get("shadow")]
    if not recs:
        return {"requests": 0, "note": "No shadow-mode traffic recorded yet."}
    actual = sum(r.get("cost_usd") or 0 for r in recs)
    projected = sum(r["shadow"]["projected_cost_usd"] for r in recs)
    would_cache = sum(1 for r in recs if r["shadow"]["would_cache"])
    models = {}
    for r in recs:
        m = r["shadow"]["projected_model"] or "cache"
        models[m] = models.get(m, 0) + 1
    q = [r["shadow"]["projected_quality"] for r in recs if r["shadow"].get("projected_quality")]
    return {
        "requests": len(recs), "basis": "estimated",
        "current_spend_usd": round(actual, 6), "projected_spend_usd": round(projected, 6),
        "projected_saving_usd": round(actual - projected, 6),
        "projected_saving_pct": round(100 * (actual - projected) / actual, 1) if actual else 0.0,
        "would_cache": would_cache, "projected_model_mix": models,
        "projected_avg_quality": round(sum(q) / len(q), 2) if q else None,
        "incumbent_avg_quality": (lambda v: round(sum(v) / len(v), 2) if v else None)(
            [r["quality_score"] for r in recs if r.get("quality_score") is not None]),
        "note": "Production traffic was served by the incumbent model. Projected spend is estimated "
                "from the versioned price registry using the incumbent's own token counts.",
        "samples": [{"request_id": r["request_id"], "query": r.get("query"), **r["shadow"]}
                    for r in recs[-25:]],
    }


# ================================================================ registry & policy

@app.get("/api/models")
async def api_models(p: Principal = Depends(principal)):
    reg = get_registry()
    recs = aggregate.live(await records_for(p))
    stats = pipeline.learned_stats(recs)
    return {**reg.public(), "measured": stats["quality"], "measured_output": stats["output"],
            "usage": aggregate.routing(recs)["by_model"]}


@app.get("/api/policies")
async def api_policies(p: Principal = Depends(principal)):
    cat = get_policies().catalogue()
    if not p.admin:
        cat["tenants"] = [t for t in cat["tenants"] if t["id"] == p.tenant_id]
    return cat


class PolicyPatch(BaseModel):
    tenant_id: str
    application_id: str
    patch: dict | None = None


@app.put("/api/policies")
async def api_policy_put(req: PolicyPatch, p: Principal = Depends(admin)):
    policies = get_policies()
    policies.set_override(req.tenant_id, req.application_id, req.patch)
    eff = policies.resolve(req.tenant_id, req.application_id)
    return {"applied": req.patch, "effective": eff.public()}


class FlagPatch(BaseModel):
    name: str
    value: bool | None = None


@app.put("/api/flags")
async def api_flag_put(req: FlagPatch, p: Principal = Depends(admin)):
    try:
        get_policies().set_flag(req.name, req.value)
    except KeyError as e:
        raise HTTPException(400, str(e))
    flags, prov = get_policies().flags()
    return {"flags": {k: {"value": v, "source": prov[k]} for k, v in flags.items()}}


@app.get("/api/flags")
async def api_flags(p: Principal = Depends(principal)):
    flags, prov = get_policies().flags()
    return {"flags": {k: {"value": v, "source": prov[k]} for k, v in flags.items()}}


@app.get("/api/budgets")
async def api_budgets(p: Principal = Depends(principal)):
    policies = get_policies()
    out = []
    for t in policies.catalogue()["tenants"]:
        if not p.admin and t["id"] != p.tenant_id:
            continue
        for a in t["applications"]:
            out.append({"tenant_id": t["id"], "application_id": a["id"], "label": a["label"],
                        "budgets": [b.public() for b in await budget_mod.status(policies, t["id"], a["id"])]})
    return {"scopes": out}


class BudgetPatch(BaseModel):
    tenant_id: str
    application_id: str
    daily_usd: float | None = None
    monthly_usd: float | None = None
    reset: bool = False


@app.put("/api/budgets")
async def api_budget_put(req: BudgetPatch, p: Principal = Depends(admin)):
    policies = get_policies()
    if req.reset:
        await budget_mod.reset(req.tenant_id)
    patch = {"budget": {k: v for k, v in (("daily_usd", req.daily_usd),
                                          ("monthly_usd", req.monthly_usd)) if v is not None}}
    if patch["budget"]:
        policies.set_override(req.tenant_id, req.application_id, patch)
    return {"budgets": [b.public() for b in
                        await budget_mod.status(policies, req.tenant_id, req.application_id)]}


@app.get("/api/tasks")
async def api_tasks():
    return {"tasks": tasks_mod.public(), "families": tasks_mod.FAMILIES}


@app.get("/api/health/providers")
async def api_provider_health(p: Principal = Depends(principal)):
    return get_health().public()


# =================================================================== simulators

@app.post("/api/simulate")
async def api_simulate(params: dict, p: Principal = Depends(principal)):
    return simulator.simulate(params, get_registry())


@app.get("/api/simulate/defaults")
async def api_simulate_defaults(p: Principal = Depends(principal)):
    return simulator.defaults_from(await records_for(p), get_registry())


@app.post("/api/simulate/build-vs-buy")
async def api_build_vs_buy(params: dict, p: Principal = Depends(principal)):
    return simulator.build_vs_buy(params, get_registry())


# ======================================================================== demo

@app.get("/api/demo/scenarios")
async def api_scenarios():
    return {"scenarios": demo.catalogue()}


class ScenarioRun(BaseModel):
    params: dict = Field(default_factory=dict)


@app.post("/api/demo/scenario/{key}")
async def api_run_scenario(key: str, req: ScenarioRun | None = None, p: Principal = Depends(admin)):
    c = require_pool()
    try:
        return await demo.run_scenario(key, pipeline, settings, c, (req.params if req else {}))
    except KeyError as e:
        raise HTTPException(404, str(e))
    except LLMError as e:
        raise HTTPException(502, str(e))


class ChaosRequest(BaseModel):
    provider: str
    mode: str | None = None


@app.post("/api/demo/chaos")
async def api_chaos(req: ChaosRequest, p: Principal = Depends(admin)):
    try:
        get_health().inject(req.provider, req.mode)
    except ValueError as e:
        raise HTTPException(400, f"{e}; modes: {CHAOS_MODES}")
    return get_health().public()


@app.post("/api/demo/reset")
async def api_demo_reset(full: bool = False, p: Principal = Depends(admin)):
    out = await demo.reset(full)
    pipeline.invalidate_learned()
    return out


# ================================================================== evaluation

class ExperimentRequest(BaseModel):
    limit: int | None = None
    full: bool = False
    fresh: bool = True
    compression: str = "off"
    strategies: list | None = None
    workloads: list | None = None


@app.post("/api/experiment/run")
async def api_experiment_run(req: ExperimentRequest, p: Principal = Depends(admin)):
    require_pool()
    prev = await experiment.current()
    if prev["status"] == "running" and prev["completed"] < prev["total"]:
        raise HTTPException(409, "experiment already running")
    modes = experiment.COMPRESSION_CHOICES.get(req.compression)
    if modes is None:
        raise HTTPException(400, "compression must be one of: off, headroom, both")
    if "headroom" in modes and not compression.available():
        raise HTTPException(409, "headroom-ai is not installed on this host. Run the compression A/B "
                                 "locally: python run_experiment.py --compression both "
                                 "(with DATABASE_URL set to share results).")
    limit = None if req.full else (req.limit or settings.demo_query_count)
    try:
        state = await experiment.start(settings, limit, req.fresh, modes,
                                       tuple(req.strategies) if req.strategies else None,
                                       tuple(req.workloads) if req.workloads else None)
    except ValueError as e:
        raise HTTPException(400, str(e))
    strategies = req.strategies or STRATEGIES
    return {"status": "started", "total_requests": state["total"],
            "queries": state["total"] // max(1, len(strategies) * len(modes)),
            "compression": list(modes), "strategies": list(strategies)}


@app.post("/api/experiment/step")
async def api_experiment_step():
    if (await experiment.current())["status"] != "running":
        return await experiment.current()
    return await experiment.step(settings, require_pool())


@app.get("/api/experiment/status")
async def api_experiment_status():
    return await experiment.current()


@app.get("/api/experiment/report", response_class=PlainTextResponse)
async def api_experiment_report():
    state = await experiment.current()
    if not state.get("result"):
        raise HTTPException(404, "no completed experiment")
    return experiment.format_report(state["result"])


@app.get("/api/classifier")
async def api_classifier(p: Principal = Depends(principal)):
    clf = classifier_mod.get_classifier()
    return {"router_version": classifier_mod.ROUTER_VERSION,
            "trained_on": clf.trained_on,
            "accuracy": clf.leave_one_out_accuracy(),
            "note": "Leave-one-out accuracy on the labelled query set: each query is predicted by a "
                    "model trained without it."}


@app.post("/api/classifier/retrain")
async def api_classifier_retrain(p: Principal = Depends(admin)):
    """Distil the LLM router's logged decisions into the free local rung."""
    recs = await storage.all_records()
    clf = classifier_mod.TaskClassifier().train_labelled()
    n = clf.train_logged(recs)
    classifier_mod._default = clf
    return {"learned_from_logged_decisions": n, "trained_on": clf.trained_on,
            "accuracy": clf.leave_one_out_accuracy()}


class EvaluateRequest(BaseModel):
    request_id: str | None = None


@app.post("/api/evaluate")
async def api_evaluate(req: EvaluateRequest, p: Principal = Depends(admin)):
    c = require_pool()
    tests = {t["query"].strip().lower(): t for t in telemetry.load_test_queries()}
    targets = await storage.all_records()
    if req.request_id:
        targets = [r for r in targets if r["request_id"] == req.request_id]
        if not targets:
            raise HTTPException(404, f"request {req.request_id} not found")
    else:
        targets = [r for r in targets if r.get("quality_score") is None and not r.get("rejected")]
    evaluated = 0
    for r in targets:
        tq = tests.get((r.get("query") or "").strip().lower())
        if not tq or not r.get("answer"):
            continue
        result = await evaluator.evaluate(r["query"], tq["expected_answer"], tq["criteria"],
                                          r["answer"], settings, c)
        await storage.update(r["request_id"], result)
        evaluated += 1
    pipeline.invalidate_learned()
    return {"evaluated": evaluated, "quality": quality_breakdowns(await storage.all_records())}


# ================================================================ legacy views

@app.get("/api/metrics")
async def api_metrics(p: Principal = Depends(principal)):
    return overview(await records_for(p))


@app.get("/api/comparison")
async def api_comparison(p: Principal = Depends(principal)):
    return comparison(baseline_records(await records_for(p)))


@app.get("/api/compression")
async def api_compression(p: Principal = Depends(principal)):
    demo_qs = telemetry.select_queries(settings.demo_query_count)
    naive = sum(compression.naive_json_tokens(t["query"]) for t in demo_qs)
    ours = sum(compression.count_tokens(telemetry.build_context(t["query"])) for t in demo_qs)
    return {
        "available": compression.available(),
        "naive_json_reference": {
            "queries": len(demo_qs), "naive_json_tokens": naive, "pipeline_tokens": ours,
            "saved_pct": round(100 * (naive - ours) / naive, 1) if naive else 0.0,
        },
        **compression_breakdown(await records_for(p)),
    }


@app.get("/api/testqueries")
async def api_testqueries():
    return telemetry.load_test_queries()


@app.get("/api/data/{name}")
async def api_data(name: str):
    ds = telemetry.load_dataset()
    if name not in ds:
        raise HTTPException(404, f"use one of: {', '.join(ds)}")
    return ds[name]


@app.get("/api/config")
async def api_config(p: Principal = Depends(principal)):
    reg = get_registry()
    policies = get_policies()
    flags, flag_src = policies.flags()
    return {
        "tiers": {k: {"name": t.name, "provider": t.provider,
                      "input_cost_per_1m": t.input_cost_per_1m,
                      "output_cost_per_1m": t.output_cost_per_1m,
                      "max_latency_ms": t.max_latency_ms,
                      "max_context_tokens": t.max_context_tokens,
                      "pricing_note": t.pricing_note}
                  for k, t in settings.tiers.items()},
        "demo_query_count": settings.demo_query_count,
        "total_query_count": len(telemetry.load_test_queries()),
        "compression_available": compression.available(),
        "router_model": settings.router.name,
        "evaluator_model": settings.evaluator.name,
        "providers_configured": pool.providers,
        "api_key_configured": bool(pool.providers),
        "storage_backend": storage.backend(),
        "price_registry_version": reg.version,
        "price_effective_date": reg.effective_date,
        "policy_version": policies.version,
        "engine_version": "optimizer-1",
        "strategies": STRATEGIES,
        "flags": {k: {"value": v, "source": flag_src[k]} for k, v in flags.items()},
        "auth": {"enabled": auth_enabled(),
                 "note": ("Requests are authenticated with virtual API keys."
                          if auth_enabled() else
                          "No API keys are configured, so this instance is OPEN: anyone who can reach "
                          "it can spend your provider credit. Set OPTIMIZER_KEY_<NAME> to enable "
                          "tenant authentication.")},
        "principal": {"tenant_id": p.tenant_id, "application_id": p.application_id,
                      "admin": p.admin, "authenticated": p.authenticated},
        "semantic_cache": cache_mod.measure_separation(),
    }


NO_STORE = {"Cache-Control": "no-store, max-age=0"}


@app.get("/")
async def index():
    return FileResponse(FRONTEND_DIR / "index.html", headers=NO_STORE)


class NoCacheStatic(StaticFiles):
    """The dashboard is served from source with no build step, so a cached
    module means an edit that appears not to have happened."""

    def is_not_modified(self, response_headers, request_headers) -> bool:
        return False

    async def get_response(self, path, scope):
        r = await super().get_response(path, scope)
        r.headers.update(NO_STORE)
        return r


app.mount("/static", NoCacheStatic(directory=FRONTEND_DIR), name="static")
