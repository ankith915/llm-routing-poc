# LLM Routing POC — Cost-Aware Model Routing for IT Operations

Proof-of-concept demonstrating that **intelligent LLM routing reduces inference
cost while maintaining answer quality**. A simulated IT Operations Assistant
answers questions about telemetry (logs, metrics, incidents) and a routing
service picks the model per request.

> The objective isn't to always use the cheapest model. It's to use the
> cheapest model that can satisfy the requirements of the request.

## Three strategies compared

| Strategy | How it routes |
|---|---|
| `none` | Baseline — every query goes to the premium model |
| `rule` | Deterministic keyword heuristics → SIMPLE/MEDIUM/HARD → cheap/medium/premium |
| `intelligent` | A small LLM produces a routing decision (complexity, reasoning, context, quality, latency requirements); code then selects the **cheapest configured model satisfying every requirement** |

All strategies see identical context per query, and every answer is graded 1–5
by an LLM judge against a predefined expected answer — so cost savings are
never claimed without measuring quality.

## Quick start

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env          # add GROQ_API_KEY and OPENAI_API_KEY
.venv/bin/uvicorn backend.api.main:app --reload
```

**Getting a Groq key**: sign in at [console.groq.com](https://console.groq.com)
→ **API Keys** → Create API Key.

**Groq rate limits matter.** On the **free** plan the gpt-oss models allow
30 RPM / 1K RPD but only **8,000 tokens per minute**. One answer call carries
~1.7k tokens, so the free plan puts a **~7.7 minute floor** on an 18-query run
no matter how much concurrency you use — raising it just produces 429s. The
[Developer plan](https://console.groq.com/settings/billing/plans) is
pay-as-you-go with no monthly fee and lifts the ceiling; the same run then
finishes in about a minute and costs roughly **$0.02** in Groq tokens.

`EXPERIMENT_CONCURRENCY` defaults to **2**, which is right for the free plan.
Raise it to 8 only after upgrading — on the free plan a higher value just
collects 429s and back-off waits, making the run slower, not faster.

Open http://localhost:8000 — the dashboard has four views:
**Experiment results** (cost saving, quality retention, model utilization,
quality-vs-cost scatter, strategy table), **Query console** (route a single
query and inspect the decision), **Request log**, and **Telemetry data**.

### Run the batch experiment

Dashboard: click **Run experiment** for the quick 18-query demo set, or
**Full run** for all 36. Or CLI:

```bash
.venv/bin/python run_experiment.py            # quick set: 18 queries × 3 strategies
.venv/bin/python run_experiment.py --full     # all 36 queries × 3 strategies
.venv/bin/python run_experiment.py --limit 9  # smaller stratified subset
```

Any subset is **stratified** across SIMPLE/MEDIUM/HARD. This matters: the
dataset is stored in complexity order, so a plain `queries[:18]` would return
14 SIMPLE, 4 MEDIUM and zero HARD — every strategy would pick the cheap tier
and the comparison would show no difference at all.

This executes each selected query through all three strategies, then prints:

```
=================================================
              EXPERIMENT RESULTS
=================================================
Queries per strategy                         36

                        Cost   Quality   Latency
-------------------------------------------------
No routing (bas       $x.xxxx     x.xx      x.xs
Rule-based rout       $x.xxxx     x.xx      x.xs
Intelligent rou       $x.xxxx     x.xx      x.xs
-------------------------------------------------
Intelligent Routing Cost Saving: xx.x%
Quality Retention:               xx.x%
```

All numbers are computed from actual token usage returned by the provider and
the prices in `backend/config/models.yaml` — nothing is hard-coded.

### Docker

```bash
cp .env.example .env   # add your key
docker compose up --build
```

## Context compression (token reduction)

Routing decides *which model* answers; compression decides *how many tokens*
it reads. They are independent levers, and the dashboard keeps them separate:
every headline number is computed with compression **off**, and a dedicated
panel reports the compression effect on its own.

### The win the pipeline already banks

Most teams put telemetry into the prompt by dumping the raw dataset as JSON.
`build_context` formats it as one `key=value` line per sample instead. Measured
with the model's own tokenizer (`o200k_base`) across the 18 demo queries:

| Context format | Tokens | vs naive JSON |
|---|---|---|
| Naive compact JSON dump | 39,495 | — |
| **Pipeline format (what is sent)** | **29,298** | **−25.8%** |
| TOON encoding of the same data | 37,000+ | −5% large / **+18% small** |

TOON (Token-Oriented Object Notation) was evaluated and rejected for this
data: its win is uniform tabular JSON, and the pipeline's line format is already
denser. The naive-JSON comparison is live on the dashboard (`GET
/api/compression`) — a measurement, not a run.

### Headroom, measured honestly

[Headroom](https://github.com/headroomlabs-ai/headroom) compresses tool outputs,
logs and RAG chunks before they reach the model. It is a serious project
(Apache-2.0, 70k+ stars). Its headline 60–95% figure is for verbose JSON; on
this pipeline's already-dense format the only transform it finds is a
**timestamp prefix fold** — `2026-08-18T10:18:00 …` becomes a hoisted
`2026-08-18T10` header with `18:00 …` rows beneath it. No profile
(`coding`, `general`, `balanced`, `agent-90`) compresses without doing that
fold, and none does it while keeping timestamps intact.

Run as an A/B — every query × strategy once uncompressed and once through
Headroom, 108 graded requests, 2026-09-08:

| | Context tokens | Cost / request | Quality (1–5) |
|---|---|---|---|
| No routing (all `gpt-4.1`) | −17.6% | **−9.9%** | **+0.06** |
| Rule-based | −17.6% | −11.5% | −0.56 |
| Intelligent | −17.6% | −24.5%¹ | −0.44 |

The quality damage is not about question difficulty — HARD questions barely
moved. It is about **which model** reads the folded context:

| Model | Quality, off → on |
|---|---|
| `gpt-4.1` (premium) | 4.59 → 4.61 (+0.02) |
| `gpt-oss-120b` (medium) | 4.73 → 4.50 (−0.23) |
| `gpt-oss-20b` (cheap) | 4.64 → **3.57 (−1.07)** |

A frontier model reassembles the folded timestamps without noticing; the 20B
model misreads them (`"At 20:00 T, payment-api latency jumps…"`) and loses the
incident timeline. **Compression and cheap-tier routing interact badly**: the
queries routed to the weakest model are exactly the ones compression breaks.

The actionable conclusion — and the Phase-2 item this suggests — is a policy,
not a switch: *compress context for the medium and premium tiers; send the
cheap tier the original.* On this data that keeps the ~10% saving where it is
free and removes the quality hit entirely.

¹ Intelligent's larger saving includes one query the router moved from premium
to medium once it saw a smaller context. Treat ~10% as the robust figure.

### Running the A/B

Headroom's dependency tree (litellm, botocore, …) is over Vercel's 250 MB
function limit, so it is **not** in `requirements.txt` and the deployed app
refuses to queue a Headroom run (HTTP 409) rather than silently write
"compressed" records that were sent uncompressed. Run it locally:

```bash
.venv/bin/pip install -r requirements-compression.txt
.venv/bin/python run_experiment.py --compression both     # ~10 min on Groq free plan
```

Results land in whatever store `.env` points at. Set `DATABASE_URL` to the
production Postgres and the deployed dashboard's compression panel populates.

`backend/compression.py` is the whole stage. Mode `off` is the identity; mode
`headroom` never raises — a compressor failure or a host without `headroom-ai`
degrades to a passthrough and records why on the request (`compression_error`).

## Configuration

`backend/config/models.yaml` — model tiers, providers, prices (USD / 1M
tokens), latency and context limits. Swap any model id without touching code.

| Tier | Model | Provider | Pricing (USD / 1M in / out) |
|---|---|---|---|
| cheap | `openai/gpt-oss-20b` | Groq | $0.075 / $0.30 |
| medium | `openai/gpt-oss-120b` | Groq | $0.15 / $0.60 |
| premium | `gpt-4.1` | OpenAI | $2.00 / $8.00 |
| router + judge | `gpt-4o-mini` | OpenAI | $0.15 / $0.60 |

**Every price here is a real billed rate**, so the cost comparison is actual
dollars on both sides — there is no "free endpoint costed at a reference
price" asterisk to explain. Model ids and prices verified against Groq's live
`/models` endpoint and `console.groq.com/docs/models` on **2026-09-02**;
re-verify before quoting numbers.

### Why `reasoning_effort: low` is pinned

The `gpt-oss` models are **reasoning models**. Left at their default they spend
170–215 completion tokens on hidden reasoning before writing anything, which at
`max_tokens: 900` truncates the visible answer mid-sentence (`finish_reason:
"length"`) — and sometimes returns an empty string outright. The LLM judge then
grades that truncated text harshly, and the quality-retention headline collapses
for reasons that have nothing to do with routing.

Setting `reasoning_effort: low` in `models.yaml` cuts reasoning to ~25–30 tokens
and returns complete answers. Measured on 2026-09-02:

| Model | reasoning tokens | finish_reason |
|---|---|---|
| gpt-oss-120b, default | 214 | `length` (truncated) |
| gpt-oss-120b, `low` | 32 | `stop` |
| gpt-oss-20b, default | 170 | `stop` |
| gpt-oss-20b, `low` | 25 | `stop` |

Don't remove it without re-measuring. As a second line of defence, an empty
completion is treated as a failed call and triggers the fallback chain below.

### Fallback chain

A single flaky provider call must not drop a cell from the comparison, so
`pipeline.answer_with_fallback` tries the routed tier, then escalates
(cheap → medium → premium), then descends if nothing above is left. Cost is
attributed to the tier that **actually answered**, so a fallback can never
understate real spend. The record carries `routed_tier` and `fallback_used`
alongside `tier` for auditing.

Only a query that exhausts every tier counts as failed. The dashboard
deliberately does not render provider errors — they go to the server log and
the CLI — because a stack trace on screen during a client demo is worse than
useless.

`.env` (see `.env.example`):

| Var | Purpose |
|---|---|
| `GROQ_API_KEY` | cheap + medium tiers |
| `OPENAI_API_KEY` | premium tier + router + judge |
| `GROQ_BASE_URL` / `OPENAI_BASE_URL` | endpoint overrides |
| `ROUTER_MODEL` / `EVALUATOR_MODEL` | override the yaml defaults |
| `DEMO_QUERY_COUNT` | queries in a quick run (default 18) |
| `EXPERIMENT_CONCURRENCY` | parallel LLM calls in batch runs (default **2**, sized for Groq's free plan; 8 after upgrading) |
| `DATABASE_URL` | Postgres DSN. Set → results go to Postgres; unset → flat file |
| `EXPERIMENT_STEP_SECONDS` | time budget for one experiment step (default 50) |

## API

| Endpoint | Description |
|---|---|
| `POST /api/query` | `{query, strategy: none\|rule\|intelligent, evaluate}` → answer + routing decision + usage + cost/latency |
| `GET /api/requests` | request log (`?strategy=` filter) |
| `GET /api/metrics` | aggregate metrics + quality breakdowns |
| `GET /api/comparison` | per-strategy cost/quality/latency + savings % |
| `POST /api/evaluate` | grade a request (or all ungraded) with the LLM judge |
| `POST /api/experiment/run` | queue the batch experiment (`{full: true}` for all 36 queries; `{compression: "both"}` for the Headroom A/B, local only) |
| `GET /api/compression` | naive-JSON reference plus the Headroom on/off breakdown, if run |
| `POST /api/experiment/step` | run the next batch of queued jobs (~50s), return progress |
| `GET /api/experiment/status` | progress, without doing work |
| `GET /api/experiment/report` | plain-text comparison report |
| `GET /api/testqueries`, `GET /api/data/{logs,metrics,incidents}`, `GET /api/config` | datasets & config |

## Dataset

`backend/data/` holds simulated telemetry covering normal service, high CPU,
high memory, DB timeout, API latency, service failure, network failure,
cascading failure and simultaneous issues. The narrative: an ad-hoc analytics
batch job on `db-primary-01` (10:18) causes slow queries → connection-pool
exhaustion → `payment-api` timeouts and circuit breaker → `checkout-web` 502s,
while `inventory-svc` (network) and `search-svc` (memory) fail independently.

`test_queries.json` contains 36 queries (14 simple / 12 medium / 10 hard),
each with an expected answer and grading criteria grounded in that data.

## Tests

```bash
.venv/bin/python -m pytest tests/ -q
```

68 offline tests cover the rule router, intelligent tier selection, cost
formulas, savings/retention math, percentiles, dataset consistency, stratified
query selection, the fallback chain, and context compression (including the
naive-JSON claim, so the 25.8% figure cannot drift from the code). They run against the flat-file store
with a fake LLM client, so neither a database nor an API key is required.

## Project structure

```
llm-routing-poc/
├── backend/
│   ├── api/main.py              # FastAPI app
│   ├── router/rule_router.py    # Strategy B
│   ├── router/intelligent_router.py  # Strategy C
│   ├── llm/client.py            # one OpenAI-compatible client for all providers
│   ├── evaluation/evaluator.py  # LLM judge
│   ├── evaluation/metrics.py    # cost + aggregate metrics
│   ├── experiment.py            # batch runner (resumable steps)
│   ├── compression.py           # optional context-compression stage (Headroom)
│   ├── pipeline.py              # route → compress → answer → cost → grade
│   ├── telemetry.py             # dataset + context builder
│   ├── storage.py               # request store: Postgres or flat file
│   ├── db.py                    # Postgres pool + schema
│   ├── data/*.json              # simulated telemetry + test queries
│   └── config/models.yaml       # model tiers & pricing
├── frontend/index.html          # dashboard (no build step)
├── api/index.py                 # Vercel entrypoint (serves the ASGI app)
├── tests/
├── run_experiment.py
├── requirements-compression.txt # local-only extras for the Headroom A/B
├── Dockerfile / docker-compose.yml
├── vercel.json
└── .env.example
```

## Deploy (Vercel)

Live: https://llm-routing-poc.vercel.app

```bash
vercel link --yes --project llm-routing-poc
vercel env add GROQ_API_KEY production      # plus OPENAI_API_KEY and DATABASE_URL
vercel deploy --prod --yes
```

Serverless imposes two constraints this app had to be adapted to, and both
adaptations are also fine to run locally:

**No writable disk, no shared memory.** Set `DATABASE_URL` and `storage.py`
keeps requests, and the experiment's progress, in Postgres instead of
`results.json` and process globals. Without it the flat-file backend is used,
so tests and `run_experiment.py` need no database. Request ids come from a
Postgres sequence so concurrent instances can't collide.

**A function is frozen the moment it responds**, so a background task started
with `create_task` would never finish. The batch experiment is therefore split
into resumable steps: `POST /api/experiment/run` queues the jobs, and each
`POST /api/experiment/step` drains as many as fit in `EXPERIMENT_STEP_SECONDS`
before persisting progress and returning. The dashboard's poll loop drives it,
and a Postgres advisory lock stops two tabs running the same jobs twice.

Note that the production URL is public by default — it spends your OpenAI key
on every request. Enable Deployment Protection under Project → Settings →
Deployment Protection to gate it behind your Vercel login.

## Phase 2 (out of scope here)

Tier-aware compression (compress for medium/premium, not cheap — see above),
budget-aware routing, latency-aware preferences and context-aware overrides
are partially anticipated (`select_tier` already
enforces context windows and latency ceilings) but budget tracking and
fallback are deliberately not built until the routing economics are proven.
