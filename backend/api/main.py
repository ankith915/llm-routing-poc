"""FastAPI application for the LLM routing POC."""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from backend import compression, db, experiment, storage, telemetry
from backend.config.loader import load_settings
from backend.evaluation import evaluator
from backend.evaluation.metrics import (baseline_records, comparison, compression_breakdown,
                                        overview, quality_breakdowns)
from backend.llm.client import ClientPool, LLMError
from backend.pipeline import run_query

FRONTEND_DIR = Path(__file__).resolve().parent.parent.parent / "frontend"

settings = load_settings()
pool: ClientPool = ClientPool({})


@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool
    await storage.init()
    pool = ClientPool.from_settings(settings)
    yield
    await pool.aclose()
    await db.close()


app = FastAPI(title="LLM Routing POC", lifespan=lifespan)


def require_pool() -> ClientPool:
    if not pool.providers:
        raise HTTPException(503, "No API keys configured. Copy .env.example to .env and set "
                                 "GROQ_API_KEY and OPENAI_API_KEY.")
    return pool


class QueryRequest(BaseModel):
    query: str
    strategy: str = "intelligent"   # none | rule | intelligent
    evaluate: bool = False


class EvaluateRequest(BaseModel):
    request_id: str | None = None   # omit to evaluate all unevaluated test-query requests


class ExperimentRequest(BaseModel):
    # None means "the quick demo subset"; pass full=True for the whole dataset.
    limit: int | None = None
    full: bool = False              # run all test queries instead of the demo subset
    fresh: bool = True              # clear previous results first
    compression: str = "off"        # off | headroom | both (A/B: every job run both ways)


@app.post("/api/query")
async def api_query(req: QueryRequest):
    if req.strategy not in ("none", "rule", "intelligent"):
        raise HTTPException(400, "strategy must be one of: none, rule, intelligent")
    c = require_pool()
    try:
        r = await run_query(req.query, req.strategy, settings, c, evaluate=req.evaluate)
    except LLMError as e:
        raise HTTPException(502, str(e))
    return {
        "request_id": r["request_id"],
        "answer": r["answer"],
        "routing": r["routing"],
        "usage": {"input_tokens": r["input_tokens"], "output_tokens": r["output_tokens"]},
        "metrics": {"latency_ms": r["latency_ms"], "cost_usd": r["cost_usd"]},
        "model": r["model"],
        "quality_score": r["quality_score"],
        "quality_rationale": r["quality_rationale"],
    }


@app.get("/api/requests")
async def api_requests(strategy: str | None = None, limit: int = 500):
    records = await storage.all_records()
    if strategy:
        records = [r for r in records if r["strategy"] == strategy]
    return {"count": len(records), "requests": records[-limit:][::-1]}


@app.get("/api/metrics")
async def api_metrics():
    return overview(await storage.all_records())


@app.get("/api/comparison")
async def api_comparison():
    # Routing-only view: compression is a separate lever, reported by /api/compression.
    return comparison(baseline_records(await storage.all_records()))


@app.get("/api/compression")
async def api_compression():
    """The token-reduction story: the win the context format already banks, and
    the Headroom on/off A/B if one has been run."""
    demo = telemetry.select_queries(settings.demo_query_count)
    naive = sum(compression.naive_json_tokens(t["query"]) for t in demo)
    ours = sum(compression.count_tokens(telemetry.build_context(t["query"])) for t in demo)
    return {
        "available": compression.available(),
        "naive_json_reference": {
            "queries": len(demo),
            "naive_json_tokens": naive,
            "pipeline_tokens": ours,
            "saved_pct": round(100 * (naive - ours) / naive, 1) if naive else 0.0,
        },
        **compression_breakdown(await storage.all_records()),
    }


@app.post("/api/evaluate")
async def api_evaluate(req: EvaluateRequest):
    c = require_pool()
    tests = {t["query"].strip().lower(): t for t in telemetry.load_test_queries()}
    targets = await storage.all_records()
    if req.request_id:
        targets = [r for r in targets if r["request_id"] == req.request_id]
        if not targets:
            raise HTTPException(404, f"request {req.request_id} not found")
    else:
        targets = [r for r in targets if r.get("quality_score") is None]
    evaluated = 0
    for r in targets:
        tq = tests.get(r["query"].strip().lower())
        if not tq:
            continue
        result = await evaluator.evaluate(r["query"], tq["expected_answer"], tq["criteria"],
                                          r["answer"], settings, c)
        await storage.update(r["request_id"], result)
        evaluated += 1
    return {"evaluated": evaluated, "quality": quality_breakdowns(await storage.all_records())}


@app.post("/api/experiment/run")
async def api_experiment_run(req: ExperimentRequest):
    require_pool()
    prev = await experiment.current()
    if prev["status"] == "running" and prev["completed"] < prev["total"]:
        raise HTTPException(409, "experiment already running")
    modes = experiment.COMPRESSION_CHOICES.get(req.compression)
    if modes is None:
        raise HTTPException(400, "compression must be one of: off, headroom, both")
    if "headroom" in modes and not compression.available():
        # Headroom's dependency tree is over the serverless size limit, so the
        # A/B is run locally against the same store. Refuse rather than write
        # "headroom" records that were silently sent uncompressed.
        raise HTTPException(409, "headroom-ai is not installed on this host. Run the "
                                 "compression A/B locally: python run_experiment.py "
                                 "--compression both (with DATABASE_URL set to share results).")
    limit = None if req.full else (req.limit or settings.demo_query_count)
    state = await experiment.start(settings, limit, req.fresh, modes)
    return {"status": "started", "total_requests": state["total"],
            "queries": state["total"] // (3 * len(modes)), "compression": list(modes)}


@app.post("/api/experiment/step")
async def api_experiment_step():
    """Advance the experiment by one time-boxed batch and report progress.

    The dashboard calls this on its poll loop. Work has to happen inside a
    request because a serverless function is frozen as soon as it responds.
    """
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


@app.get("/api/testqueries")
async def api_testqueries():
    return telemetry.load_test_queries()


@app.get("/api/data/{name}")
async def api_data(name: str):
    ds = telemetry.load_dataset()
    if name not in ds:
        raise HTTPException(404, "use one of: logs, metrics, incidents")
    return ds[name]


@app.get("/api/config")
async def api_config():
    return {
        "tiers": {k: {"name": t.name, "provider": t.provider,
                      "input_cost_per_1m": t.input_cost_per_1m,
                      "output_cost_per_1m": t.output_cost_per_1m,
                      "max_latency_ms": t.max_latency_ms,
                      "max_context_tokens": t.max_context_tokens,
                      "pricing_note": t.pricing_note}
                  for k, t in settings.tiers.items()},
        "demo_query_count": settings.demo_query_count,
        "compression_available": compression.available(),
        "total_query_count": len(telemetry.load_test_queries()),
        "router_model": settings.router.name,
        "evaluator_model": settings.evaluator.name,
        "providers_configured": pool.providers,
        "api_key_configured": bool(pool.providers),
        "storage_backend": storage.backend(),
    }


@app.get("/")
async def index():
    return FileResponse(FRONTEND_DIR / "index.html")


app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")
