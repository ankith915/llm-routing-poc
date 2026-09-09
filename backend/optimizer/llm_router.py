"""Tier 2 of the decision ladder: the LLM router.

Only reached when the local classifier's confidence is below the policy
threshold. It costs real money and real latency, both of which are recorded on
the request and charged to the optimizer's overhead in the ledger, so the
router can never quietly eat the savings it produces.

Its decisions are logged with `rung: llm_router`, which is exactly the training
signal `classifier.train_logged` distils back into the free rung.
"""
import json
import re

from backend.optimizer import tasks
from backend.optimizer.classifier import Classification
from backend.llm.client import ClientPool, LLMError
from backend.optimizer import pricing

PROMPT = """You are a routing controller for an enterprise AI gateway. Classify the request.
Respond with ONLY a JSON object, no prose:

{{"task_type": <one of: {task_types}>,
  "difficulty": "SIMPLE" | "MEDIUM" | "HARD",
  "confidence": <0.0-1.0>,
  "reason": "<one short sentence>"}}

Guidance:
- SIMPLE: one factual lookup, a classification, or a field extraction. No multi-step reasoning.
- MEDIUM: explaining or comparing using one or two signals; a single-subject "why"; a summary.
- HARD: multi-step investigation across several services or signal types - root-cause chains,
  event timelines, blast-radius assessments, remediation plans.
- Most requests are SIMPLE or MEDIUM. Reserve HARD for genuine multi-step investigation.
- The context available to the assistant is about {context_tokens} tokens.

Request: {query}"""


def parse(raw: str) -> dict:
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        raise ValueError(f"no JSON object in router output: {raw[:120]}")
    d = json.loads(m.group(0))
    if "difficulty" not in d and "complexity" in d:
        d["difficulty"] = d["complexity"]
    if "difficulty" not in d:
        raise ValueError("router output missing 'difficulty'")
    d["difficulty"] = str(d["difficulty"]).upper()
    if d["difficulty"] not in ("SIMPLE", "MEDIUM", "HARD"):
        raise ValueError(f"router returned an unknown difficulty {d['difficulty']!r}")
    return d


async def classify(query: str, context_tokens: int, settings, pool: ClientPool,
                   local: Classification, registry) -> tuple:
    """Returns (Classification, cost_usd, latency_ms). Falls back to `local`."""
    router = settings.router
    prompt = PROMPT.format(query=query, context_tokens=context_tokens,
                           task_types=", ".join(sorted(tasks.TASK_TYPES)))
    try:
        resp = await pool.get(router.provider).chat(
            model=router.name, messages=[{"role": "user", "content": prompt}],
            temperature=router.params.get("temperature", 0),
            max_tokens=router.params.get("max_tokens", 300), retries=1)
    except LLMError as e:
        local.note = (local.note + "; " if local.note else "") + \
            f"LLM router unavailable ({str(e)[:60]}); kept the local classification"
        return local, 0.0, 0
    try:
        spec = registry.get(router.name)
        cost = pricing.price(spec, resp.input_tokens, resp.output_tokens, registry=registry).total_usd
    except KeyError:
        cost = (resp.input_tokens / 1e6 * router.input_cost_per_1m
                + resp.output_tokens / 1e6 * router.output_cost_per_1m)
    try:
        d = parse(resp.content)
    except (ValueError, json.JSONDecodeError) as e:
        local.note = (local.note + "; " if local.note else "") + \
            f"LLM router returned unusable output ({str(e)[:60]}); kept the local classification"
        return local, cost, resp.latency_ms
    task_type = d.get("task_type") if d.get("task_type") in tasks.TASK_TYPES else local.task_type
    diff = d["difficulty"]
    floor = tasks.get(task_type).default_difficulty
    note = d.get("reason", "")[:160]
    if tasks.DIFFICULTY_RANK[diff] < tasks.DIFFICULTY_RANK[floor]:
        diff = floor
        note += f" (raised to the task's floor {floor})"
    conf = float(d.get("confidence", 0.8) or 0.8)
    return Classification(task_type=task_type, difficulty=diff, confidence=conf,
                          task_confidence=conf, difficulty_confidence=conf, rung="llm_router",
                          required_capabilities=tasks.get(task_type).required_capabilities,
                          alternatives=local.alternatives,
                          note=f"local classifier was unsure ({local.confidence:.2f}); LLM router: {note}"), \
        cost, resp.latency_ms
