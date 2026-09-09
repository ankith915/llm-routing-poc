# AI Inference Cost Optimizer

A provider-neutral control plane that decides, per request, the **cheapest safe path to the
required business outcome** — and can explain every decision afterwards.

> We are not choosing cheaper models. We are deciding whether the model call can be avoided, what
> can be left out of it, which model can safely finish the job, how much reasoning it needs, whether
> anyone is waiting for it, and whether the answer met its contract. Then we show the receipt.

Not another LLM gateway and not another router. The differentiator is the **decision and
measurement layer**: what was considered, what was rejected, what it cost, what it would have cost,
and how much of that comparison is measured rather than assumed.

Live: <https://llm-routing-poc.vercel.app>

## Documentation

| Document | For |
|---|---|
| **[Architecture & user guide (v2)](docs/ARCHITECTURE-v2.md)** | The full design: diagrams, the request pipeline, every optimization layer, and **what each dashboard page is for and what to do in it** |
| [Architecture & user guide (v1)](docs/ARCHITECTURE-v1.md) | The routing POC this evolved from, documented in the same shape |
| [Design decisions](docs/DESIGN-DECISIONS.md) | The audit of v1, how conflicting research was reconciled, and **the six places where measurement contradicted the plan** |
| This README | Quick start, configuration, API and deployment |

If you are opening the dashboard for the first time, read
[§12 of the v2 guide](docs/ARCHITECTURE-v2.md#12-the-dashboard-page-by-page) — it walks through all
fourteen pages and says what to click.

---

## Contents

- [What it does](#what-it-does)
- [Quick start](#quick-start)
- [The request pipeline](#the-request-pipeline)
- [Two measured findings that shaped the design](#two-measured-findings-that-shaped-the-design)
- [How honesty is enforced](#how-honesty-is-enforced)
- [The dashboard](#the-dashboard)
- [Demo scenarios](#demo-scenarios)
- [Configuration](#configuration)
- [API](#api)
- [Architecture](#architecture)
- [Testing](#testing)
- [Deployment](#deployment)
- [What is deliberately not built](#what-is-deliberately-not-built)

---

## What it does

For every request it answers, in order:

| Question | Layer |
|---|---|
| Can we avoid the model call entirely? | exact cache, then semantic cache with subject guards |
| What kind of work is this, and how hard? | rules → local classifier → LLM router only when unsure |
| What is the cheapest model that can safely finish it? | routing engine over the model registry |
| Can we send less? | task-aware context selection, tier-aware compression |
| How much reasoning does it need? | execution planner |
| Does anyone have to wait for it? | interactive / near-real-time / batch / offline |
| Did the answer satisfy its contract? | deterministic validators first, LLM judge only where needed |
| If not, escalate — and bill both attempts openly | escalation |
| What did it cost, and what would it have cost? | cost ledger and savings attribution |

On the shipped demo workload against real providers, the optimizer answers the same questions for
**53–72% less** than sending everything to the frontier model, at a 100% quality-gate pass rate.
Those are measurements of this synthetic IT-operations workload, not a forecast of yours — which is
the point of shadow mode below.

---

## Quick start

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
cp .env.example .env            # add GROQ_API_KEY and OPENAI_API_KEY
.venv/bin/uvicorn backend.api.main:app --reload
```

Open <http://localhost:8000>, then either:

- press **Run demo workload** on the overview, or
- open **Presentation mode** for the guided eleven-step client demo, or
- send one request from the **Live request flow** page and watch the pipeline light up.

Everything in the UI is computed from stored request records. Clear the log and the pages go empty.

```bash
.venv/bin/python -m pytest tests/ -q            # 181 offline tests, no network, ~2s
.venv/bin/python run_experiment.py --limit 9    # CLI evaluation with a printed report
```

**Groq rate limits matter.** On the free plan the `gpt-oss` models allow 30 requests/min but only
8,000 **tokens** per minute, which puts a floor of several minutes on a batch evaluation no matter
how much concurrency you use — raising it just collects 429s. `EXPERIMENT_CONCURRENCY` defaults to
`2`, which is right for the free plan; raise it to `8` after upgrading.

---

## The request pipeline

```
  request
    │
    ▼
┌─ policy ──────────── defaults < tenant < application < request; every value keeps its source
├─ budget ──────────── reject if even the cheapest route does not fit; reserve the worst case
├─ exact cache ─────── canonical hash over query + prompt/knowledge/policy version ──┐
├─ classify ────────── rules → local naive-Bayes → LLM router only below threshold   │ hit
├─ semantic cache ──── similarity AND subject AND task AND version AND freshness ────┤
├─ route ───────────── eligibility → expected cost/quality/latency → cheapest safe   │
├─ context ─────────── keep what the task reads; compress only where measured safe   │
├─ plan ────────────── execution mode, reasoning level, output budget                │
├─ execute ─────────── provider call, fallback chain, circuit breaker                │
├─ quality gate ────── validators (label/schema/SQL/groundedness) then judge         │
├─ escalate ────────── retry higher when the gate fails; both attempts billed        │
└─ ledger ──────────── actual, baseline, attribution bars ◄──────────────────────────┘
```

Three ordering decisions are deliberate and were forced by measurement:

**Caches run before classification of the prompt, but the semantic cache runs after task
classification.** Similarity alone is not answer equivalence, and the semantic guard needs to know
what kind of question this is.

**Routing runs before context optimisation.** Compression is model-dependent (see below), so the
context stage has to know which tier will read the result. The router therefore sees, and the
context-window constraint uses, the *uncompressed* token count.

**Cache lookups key on the uncompressed canonical request.** A hit must never depend on which
compression policy happened to be active when the entry was written.

### The decision ladder

The router must not cost more than it saves.

| Tier | What it is | Cost |
|---|---|---|
| 0 | Deterministic policy: sensitivity, allow/deny lists, context limits, budget | zero |
| 1 | High-precision regex rules for task type | zero |
| 2 | Local naive-Bayes classifier over word n-grams, trained on labelled queries and seed phrases | zero |
| 3 | LLM router — **only** when the local rungs fall below the policy's confidence threshold | a real API call, charged to the ledger as overhead |
| 4 | Cascade: the cheap answer is tried, and escalated only if the gate rejects it | a second call, billed and shown |

The local classifier reports **leave-one-out accuracy** on the labelled set — each query predicted by
a model trained without it — currently **93% on task type and 85% on difficulty**. The LLM router's
own logged decisions can be distilled back into the free rung from the evaluation workbench.

---

## Two measured findings that shaped the design

Both are results from this repo, both contradict how the market sells the lever, and both are why
the architecture is shaped the way it is.

### 1. Compression and cheap-tier routing interact destructively

Every query × strategy, run once uncompressed and once compressed — 108 graded requests,
2026-09-08. Context tokens fell 17.6% across the board. Quality did not move uniformly:

| Model | Quality (1–5), compression off → on |
|---|---|
| `gpt-4.1` (frontier) | 4.59 → 4.61 (+0.02) |
| `gpt-oss-120b` (balanced) | 4.73 → 4.50 (−0.23) |
| `gpt-oss-20b` (cheap) | 4.64 → **3.57 (−1.07)** |

The compressor's only available transform on an already-dense format is a timestamp prefix fold. A
frontier model silently reassembles the folded timestamps; the 20B model misreads them and loses the
incident timeline. **The damage tracks which model reads the context, not how hard the question is**
— and routing sends the most traffic to precisely the weakest model.

The resolution is a policy, not a switch: `compression.mode: tier_aware` compresses for the balanced
and frontier tiers and sends the cheap tier the original. It is also why the context stage runs
*after* routing.

### 2. Embedding similarity alone cannot make a semantic cache safe

Measured on this corpus with the local lexical embedder:

| | Similarity |
|---|---|
| Worst true paraphrase | 0.277 |
| Best *distractor* (a different question) | 0.787 |
| Best distractor **after subject guards** | 0.157 |

"The current CPU usage of payment-api" scores **0.73** against "the current CPU usage of auth-svc"
but only **0.38** against its own paraphrase. A threshold that serves the paraphrase also serves the
wrong service's number.

So a semantic hit requires every guard to pass: similarity **and** the same entities **and** the same
aspect **and** the same task type **and** the same prompt/knowledge/policy version **and** freshness
**and** that the stored answer passed its own quality gate. The guards remove 79 of 83 distractors
and make the corpus separable. Rejected candidates and the failing guard are shown in the trace, and
the separation measurement is on the cache page — nobody has to take the threshold on trust.

---

## How honesty is enforced

This is a product for a conversation where the client's first instinct is disbelief. The rules are
mechanical, not aspirational.

**Every cost figure carries its basis.**

| Basis | Meaning |
|---|---|
| `measured` | computed from token counts the provider actually returned |
| `calibrated` | estimated from the price registry, then corrected by the ratio observed on real frontier-model runs |
| `estimated` | from the price registry, with no real comparison run yet |
| `modeled` | a published rate applied to a scenario this system did not execute (batch discounts, the simulator) |

**Baselines are estimated and then calibrated, never substituted.** In production you cannot run the
baseline and the optimised path side by side without doubling the bill. Two runs of the same question
on the same model differ mainly in how long the answer happens to be, so substituting a real baseline
run's dollar cost lets a single request show a *negative* saving purely from that variance. Instead
every baseline is the frontier price applied to that request's own uncompressed input tokens and its
actual output length, and real frontier runs supply a calibration factor with its sample size and
spread.

**The savings waterfall reconciles by construction.** Bars are summed from per-request ledger rows,
and the routing / provider-cache / execution-mode bars decompose the serving cost in three
non-overlapping steps so no saving is counted twice. The UI shows whether the residual is zero; a
test asserts it.

**Quality is never inferred from cost.** Deterministic validators run first and cost nothing — an
exact label, a JSON schema with expected values, a SQL query **executed** against the demo warehouse
and compared with a reference result, a groundedness check that every number and entity in the
answer appears in the context. The LLM judge runs only where no contract exists, and judge calls are
excluded from serving cost with that stated on the page.

**The router refuses to guess.** A model with no measured quality for a difficulty band is not
eligible unless a policy explicitly allows it (`allow_unmeasured_models`, false by default). The
model economics page shows measured priors in green and assumed ones in amber.

**An unsecured instance says so.** With no `OPTIMIZER_KEY_*` configured, `/api/config` and the
sidebar both say the instance is open and anyone who reaches it spends your provider credit.

**Records that predate the ledger are set aside, not counted as free**, with the count shown.

---

## The dashboard

No build step: `frontend/index.html` plus ES modules in `frontend/app/`. Light and dark themes,
inline SVG charts, no external chart library.

| Page | What it answers |
|---|---|
| **Overview** | What is our AI costing, what did we save, is quality safe, where is the budget |
| **Savings waterfall** | Where every dollar of the difference came from, and what each bar rests on |
| **What is costing us** | Waste findings derived from your traffic, each with its evidence and sample size |
| **Recommendations** | What to change, the measured quality evidence for and against, the exact policy patch |
| **Requests & traces** | Every request; click one for the full stage-by-stage decision trace |
| **Live request flow** | The architecture lighting up as a real request moves through it |
| **Query console** | Ask, and see the routing decision above every answer |
| **Model economics** | Prices, capabilities, measured vs assumed quality, cost-quality frontier |
| **Policies & budgets** | Effective policy with the layer that set each value; feature flags; provider health |
| **Quality & gates** | Validator outcomes, gate failures, escalations, router accuracy |
| **Evaluation workbench** | Run a dataset through several strategies and compare like with like |
| **Shadow mode** | The safe adoption path: what the optimizer *would* have done, priced |
| **What-if simulator** | Modelled monthly spend, plus a build-vs-buy calculator |
| **Presentation mode** | Eleven guided beats, each executing real requests live |

---

## Demo scenarios

Each runs real requests through the real pipeline and reports what actually happened. If a scenario
cannot make its point on a given run, it says so rather than pretending.

| Scenario | Shows |
|---|---|
| `baseline` | The number to beat: every request on the frontier model |
| `optimized` | The same questions, routed on what they need |
| `cache` | An identical request served from cache, a paraphrase served from the semantic cache, and **the same question about a different service correctly refused** |
| `escalation` | A deterministic contract failure escalating to a stronger model, both attempts billed |
| `failover` | An injected provider outage absorbed without losing the answer |
| `budget` | Pressure changing the route, then a hard limit rejecting before any model is called |
| `sla` | The same work moving to the batch lane for the application with nobody waiting |
| `shadow` | Production served by the incumbent while the optimizer reports what it would have done |

```bash
curl -X POST localhost:8000/api/demo/scenario/cache
curl -X POST "localhost:8000/api/demo/reset?full=true"
```

Faults are injected into the real client, so `failover` exercises the actual fallback path rather
than a mock.

---

## Configuration

### `backend/config/models.yaml` — model and price registry

Versioned (`price_registry_version`) and dated (`effective_date`), and both travel with every ledger
record so a price change never silently rewrites history. Per model: prices including cached-input
rates, context window, capabilities, measured latency profile, reasoning-token measurements, and
quality priors per difficulty **with their source and sample size**.

`backend/optimizer/pricing.py` is the only place tokens become dollars. Nothing else multiplies a
token count by a rate.

| Tier | Model | Provider | USD / 1M in / out |
|---|---|---|---|
| cheap | `openai/gpt-oss-20b` | Groq | 0.075 / 0.30 |
| balanced | `openai/gpt-oss-120b` | Groq | 0.15 / 0.60 |
| balanced (alt) | `gpt-4.1-mini` | OpenAI | 0.40 / 1.60 (cached in 0.10) |
| frontier | `gpt-4.1` | OpenAI | 2.00 / 8.00 (cached in 0.50) |

Every price is a real billed rate, so the comparison is actual dollars on both sides. Verified
2026-09-02; re-verify before quoting to a client.

### `backend/config/policies.yaml` — policy and feature flags

Precedence: **defaults < tenant < application < runtime override < request**. Every resolved value
records the layer that set it, and an unknown key is reported rather than silently ignored.

Feature flags follow their own chain — policy file < `FLAG_<NAME>` environment variable < runtime
override — and can be toggled live from the policies page, which is the fastest way to show what a
layer was actually contributing.

### `reasoning_effort` is pinned to `low` for the gpt-oss models

They are reasoning models. At their default they spend 170–215 completion tokens on hidden reasoning
before writing anything, which truncates the visible answer or returns nothing at all. Measured
2026-09-02; `low` cuts it to 25–32 tokens. Do not remove it without re-measuring.

The planner uses those measurements: an output budget for a reasoning model includes headroom for
its hidden tokens. Without that, a 20-token classification budget produced an empty completion, the
call was treated as a failure, and the request failed over to the frontier model — costing *more*
than not capping the output at all. That regression is now pinned by a test.

---

## API

### OpenAI-compatible

An application changes its base URL and keeps its code.

```bash
curl localhost:8000/v1/chat/completions -H 'content-type: application/json' -d '{
  "model": "gpt-4.1",
  "messages": [{"role": "user", "content": "Is auth-svc currently healthy?"}],
  "optimizer": {"sla_class": "interactive", "quality_target": 4.0}
}'
```

The response is a standard chat completion plus an `optimizer` block carrying the selected model,
the reason, the cost, the baseline, the quality verdict and a `trace_url`. The same facts are on the
`x-optimizer-request-id`, `x-optimizer-model` and `x-optimizer-cost-usd` headers.

### Control plane

| Endpoint | Purpose |
|---|---|
| `POST /api/query` | Dashboard request: strategy, mode (`live`/`shadow`), policy overrides |
| `GET /api/requests`, `GET /api/requests/{id}` | Request log and the full trace |
| `GET /api/analytics/{overview,waterfall,routing,cache,quality,unit-economics,waste,recommendations,shadow}` | Everything the dashboard reads |
| `GET /api/models`, `GET /api/tasks` | Registry and task taxonomy |
| `GET/PUT /api/policies`, `GET/PUT /api/flags`, `GET/PUT /api/budgets` | Policy control |
| `POST /api/simulate`, `POST /api/simulate/build-vs-buy` | Modelled scenarios |
| `POST /api/experiment/{run,step}`, `GET /api/experiment/{status,report}` | Evaluation workbench |
| `GET /api/classifier`, `POST /api/classifier/retrain` | Router accuracy and distillation |
| `POST /api/demo/{scenario/{key},chaos,reset}` | Demo control |

Authentication is by virtual API key (`OPTIMIZER_KEY_<NAME>=secret:tenant:application`). Keys are
tenant-scoped: a tenant sees only its own requests, cannot act for another application, and cannot
change policy. The admin key can.

---

## Architecture

A modular monolith. Separate services would add operational cost without buying anything here.

```
backend/
├─ api/
│  ├─ main.py            FastAPI app: control plane + OpenAI-compatible endpoint
│  └─ auth.py            virtual API keys → tenant/application principals
├─ optimizer/
│  ├─ envelope.py        RequestEnvelope: the one shape every entry point normalises into
│  ├─ registry.py        model registry: prices, capabilities, quality priors and their sources
│  ├─ pricing.py         the only cost calculator
│  ├─ policy.py          policy resolution with provenance; feature flags
│  ├─ budget.py          soft/hard limits, reservation, reconciliation, pressure
│  ├─ tasks.py           task taxonomy (adding a task type is a data change)
│  ├─ classifier.py      rules + local naive-Bayes, with leave-one-out accuracy
│  ├─ llm_router.py      the paid rung, used only below the confidence threshold
│  ├─ embeddings.py      OpenAI embedder, or a local lexical fallback that says it is lexical
│  ├─ cache.py           exact + semantic caches, subject guards, separation measurement
│  ├─ context.py         task-aware section selection, tier-aware compression
│  ├─ router.py          eligibility, expected cost/quality/latency, explainable choice
│  ├─ planner.py         execution mode, reasoning level, output budget
│  ├─ quality.py         deterministic validators + the gate
│  ├─ health.py          provider health, circuit breaker, fault injection
│  ├─ ledger.py          cost ledger and non-overlapping savings attribution
│  └─ engine.py          stage orchestration; every stage appends a trace step
├─ analytics/            aggregation, waste analysis, recommendations, simulators
├─ evaluation/           LLM judge, metrics
├─ pipeline.py           integration: runtime singletons, shadow mode, learned statistics
├─ experiment.py         resumable batch evaluation
├─ demo.py               the eight scenarios
├─ storage.py            Postgres or flat file behind one async interface
└─ data/                 synthetic IT-operations corpus + 58 labelled queries
frontend/
├─ index.html            shell
└─ app/                  design system, shared UI vocabulary, SVG charts, one module per page
```

### Data model

One record per request carries: identity (tenant, application, environment, feature, session), task
(type, difficulty, sensitivity, SLA), cache outcome, context transforms, the full routing decision
including rejected candidates, execution details, tokens (input, cached input, output, reasoning),
cost (actual, baseline, basis, savings, attribution bars), quality (validators, gate, judge),
latency, the policy/router/price-registry versions, and the trace.

Multi-tenancy is in the domain model from the start: cache namespaces, budget scopes, policy
resolution and the request log are all tenant-scoped, and tenant isolation has tests.

---

## Testing

```bash
.venv/bin/python -m pytest tests/ -q     # 181 tests, ~2 seconds, no network
```

| File | Covers |
|---|---|
| `test_registry_and_pricing.py` | price components, cached input, batch discounts, versioning, **attribution bars sum exactly and do not overlap** |
| `test_policy_and_budget.py` | precedence and provenance, sensitivity, flags, envelope, budget admission and reservation, request-id uniqueness under concurrency |
| `test_classifier_and_quality.py` | classifier ladder, honest accuracy, every validator, gate decisions, **output budget covers hidden reasoning** |
| `test_cache_and_health.py` | cache guards and tenant isolation, semantic accept/reject reasons, circuit breaker, chaos |
| `test_pipeline_offline.py` | end-to-end per strategy, trace completeness, ledger reconciliation, cache, fallback, escalation, shadow |
| `test_scenarios.py` | each demo scenario actually demonstrates its point |
| `test_api.py` | every endpoint, tenant isolation, the OpenAI endpoint, simulators, legacy records |
| `test_compression.py`, `test_cost_and_metrics.py`, `test_selection_and_fallback.py`, `test_context_and_data.py` | the original POC's invariants, preserved |

Tests run fully offline. An autouse fixture makes constructing a real provider client raise, because
the FastAPI lifespan builds one from `.env` and a test that started the app would otherwise spend
real provider credit.

---

## Deployment

```bash
docker compose up --build          # local
```

```bash
vercel link --yes --project llm-routing-poc
vercel env add GROQ_API_KEY production     # plus OPENAI_API_KEY, DATABASE_URL, OPTIMIZER_KEY_*
vercel deploy --prod --yes
```

Serverless imposes two constraints the app is built around.

**No writable disk and no shared memory.** Set `DATABASE_URL` and requests, caches, budgets and
evaluation progress all live in Postgres instead of the flat file. Request ids come from a Postgres
sequence so concurrent instances cannot collide.

**A function is frozen the moment it responds**, so background work would never finish. Batch
evaluation is split into resumable steps: `POST /api/experiment/run` queues the jobs and each
`POST /api/experiment/step` drains as many as fit in its time budget before persisting progress. A
Postgres advisory lock stops two browser tabs running the same jobs twice.

**Set at least one `OPTIMIZER_KEY_*` on a public deployment.** Without it the instance is open and
spends your provider credit on every request.

---

## What is deliberately not built

Judgment calls worth stating rather than leaving as gaps.

**No separate gateway process.** LiteLLM or Bifrost would add real value at multi-tenant scale, and
the provider abstraction is deliberately thin enough to swap in. At this stage it would add an
operational hop without buying anything.

**No autonomous policy updates.** The learning loop proposes a policy patch with its evidence and a
confidence rating; a human approves it. A router that retrains and redeploys itself against a
quality gate it also owns is not a feature.

**No self-hosting recommendation.** The build-vs-buy calculator exists to show that self-hosting is
usually *not* cheaper at POC volume — engineering and on-call dominate the GPU bill — and it reports
the break-even volume rather than arguing a case.

**Batch execution is priced, not performed.** The planner selects the lane and applies the provider's
published discount, and every figure that depends on it is labelled `modeled` because this system
executes synchronously.

**The local embedder is lexical, and says so.** With no OpenAI key the semantic cache falls back to
bag-of-words vectors. That is a materially weaker signal, which is why the subject guards carry the
safety and the separation measurement is on screen.

---

*All figures in this repository are measurements of a synthetic IT-operations workload run against
real provider APIs at real published prices. They are a measurement of that workload, not a forecast
of yours — which is what shadow mode and the evaluation workbench are for. Prices should be
re-verified against provider pricing pages before any figure is quoted to a client.*
