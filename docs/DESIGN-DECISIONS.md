# Design decisions and the audit behind them

**Date:** 2026-09-09
**Status:** implemented
**Built on:** the routing POC at commit `d49e1a6`
**See also:** [Architecture & user guide (v2)](./ARCHITECTURE-v2.md)

This records the audit, the research reconciliation, the decisions taken, and — most usefully — the
places where measurement contradicted the plan and the design changed.

---

## 1. Audit of the existing POC

~2,500 lines: a FastAPI modular monolith plus a single-file vanilla dashboard. It was materially
better than a router demo and several of its choices survive unchanged.

| Area | Verdict | Outcome |
|---|---|---|
| `config/models.yaml` — real billed prices, dated | KEEP shape | became a versioned model **and** price registry |
| One httpx client for every OpenAI-compatible provider, retries, empty-completion guard | KEEP | extended with cached/reasoning token readback, a chaos hook and health callbacks |
| `rule_router`, `intelligent_router` | KEEP as evaluation baselines | now two of four strategies |
| Cost attributed to the tier that *actually answered* | KEEP | preserved and tested |
| Router's own cost counted | KEEP | now a negative ledger bar |
| LLM judge against ground truth | KEEP | now the last resort behind deterministic validators |
| Stratified query sampling | KEEP | extended to new workloads |
| Compression quarantined from headline numbers | KEEP | became a policy dimension |
| `pipeline.run_query` | REPLACE | staged engine with a trace |
| `metrics.calculate_cost` | REPLACE | single `optimizer.pricing` calculator |
| 527-line dashboard | REPLACE | 14-page ES-module app on a design system |
| 69 offline tests | KEEP | all preserved or evolved; now 181 |

Debt found and fixed: pricing logic duplicated; the routing decision shape was ad hoc; every
intelligent route paid an LLM call; no tenant concept; no cache, policy, budget or ledger; the
`reason` string was the only explanation. Also a genuine race — request ids were derived from the
records list before either concurrent request had appended, producing duplicates (89 unique ids
across 108 records in the shipped data).

## 2. Research reconciliation

Four research documents were supplied. Where they disagreed:

| Topic | The disagreement | Decision |
|---|---|---|
| Routing savings | 40–85%, "up to 98%" (FrugalGPT), 4.5–14.2% (Azure's own measurement), ~35% at <2% accuracy loss (RouterArena) | Quote none of them. The product reports measured savings on the workload it ran, with sample size and basis. Registry priors cite their own source. |
| Semantic cache hit rate | 15–25%, 20–45%, "up to 95%" | Hit rate is whatever the traffic produces. Report hit rate and dollars saved separately, because a high hit rate on cheap requests saves almost nothing. |
| Pipeline order | One doc: context optimiser before the semantic cache and before routing. Another: route first, compress after | Route first. Forced by our own measurement (§4). |
| Router cost | All agreed it must be cheap | Decision ladder with the paid rung gated on a confidence threshold, and its cost booked as overhead. |
| Batch discounts | 50% on OpenAI/Anthropic; Groq unverified | Applied only where the registry records a published discount, and labelled `modeled` because execution is synchronous. |
| Self-hosting | One deck implied it is cheaper | Calculator only, built to show it usually is not at POC volume. |

## 3. Architecture as built

```
policy → budget → exact cache → classify → semantic cache → route
       → context → plan → execute → quality gate → escalate → ledger
```

Each stage appends a `TraceStep(stage, status, summary, detail, cost_usd, latency_ms, tokens)`. The
trace is the explanation; the ledger is the accounting.

New package `backend/optimizer/` (17 modules), `backend/analytics/` (3), a rewritten
`backend/pipeline.py` as the integration layer, `backend/demo.py` for scenarios, and a 14-page
frontend. Module responsibilities are listed in the README.

## 4. Where measurement changed the design

These are the decisions that were not obvious from the research and would not have been made without
running the thing.

**Compression is model-dependent, so it must run after routing.** Measured over 108 graded requests:
folding context costs the cheap tier 1.07 quality points, the balanced tier 0.23, and the frontier
tier nothing. Damage tracks the reader, not the difficulty. The context stage therefore runs after
the router, takes the selected tier as input, and the router sees uncompressed token counts.

**Embedding similarity alone cannot make a semantic cache safe.** Measured on this corpus, the worst
true paraphrase scores 0.277 and the best distractor 0.787 — not separable at any threshold.
Deterministic subject guards (same entities, same aspect) drop the worst distractor to 0.157 and
remove 79 of 83 distractors. Similarity became one guard among several, and the separation
measurement is exposed in the product.

**A measured baseline is worse than a calibrated estimate.** Substituting a real frontier run's
dollar cost imports its output-length variance, so a single request can show a negative saving for no
reason connected to the optimizer. Baselines are always the frontier price on the request's own
uncompressed input tokens and actual output length; real frontier runs supply a calibration factor
(mean ratio of true to estimated, with n and spread), applied once at least three exist.

**Output budgets must include hidden reasoning.** A 20-token budget on a reasoning model produced an
empty completion, which the pipeline correctly treats as a failure — and the fallback to the frontier
model cost *more* than never capping the output. The planner now adds headroom from the registry's
measured reasoning-token counts.

**Budget pressure must reach the router, not just reject.** Reserving the premium worst case turned
every squeeze into an outage. Admission now tests the *cheapest possible* route, the reservation is
capped at what is actually left, and the remaining budget becomes the router's cost cap so it
re-routes before anything is refused.

**Attribution bars must decompose, not overlap.** The provider-cache bar began as a zero placeholder
and silently absorbed the reconciliation residual, contradicting its own explanation. Serving cost is
now split in three non-overlapping steps: routing (frontier vs selected at list price), provider
cache (list vs cached rate), execution mode (cached vs discounted).

**A demo that cannot fail proves nothing.** Three scenarios were silently short-circuited by cache
hits from earlier scenarios — the failover demo was "answered by exact-cache". Scenarios that
demonstrate routing, failover or budget now clear the cache first, and `test_scenarios.py` asserts
each one actually exercises its mechanism.

## 5. Honesty rules, enforced mechanically

- Every cost figure carries a basis: `measured`, `calibrated`, `estimated` or `modeled`.
- The waterfall reconciles by construction and the UI shows the residual; a test asserts it.
- A model with no measured quality for a difficulty band is ineligible unless a policy allows it.
- Deterministic validators before the judge; judge cost excluded from serving cost and stated.
- An instance with no API keys says it is open.
- Pre-ledger records are excluded from cost math, with the count shown.
- The classifier reports leave-one-out accuracy, not training accuracy.

## 6. Risks and trade-offs accepted

| Risk | Position |
|---|---|
| The local classifier is not an embedding model | Confidence gating plus the LLM rung bound the damage; leave-one-out accuracy is published |
| The local embedder measures lexical, not semantic, similarity | Labelled as lexical; subject guards carry the safety; separation is measured on screen |
| Batch pricing is modelled, not executed | Labelled `modeled` everywhere it appears |
| Quality priors start as assumptions | Marked amber, excluded from routing by default, replaced by measurement from the workbench |
| Groq free-plan rate limits cap demo speed | Concurrency is configurable and the constraint is documented |
| Savings are workload-specific | Shadow mode and the workbench exist precisely to replace our number with the client's |

## 7. Not built, deliberately

A separate gateway process, autonomous policy updates, a self-hosting recommendation, and real batch
execution. Reasoning for each is in the README's final section.
