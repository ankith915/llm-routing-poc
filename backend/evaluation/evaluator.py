"""LLM-as-judge quality evaluation against predefined expected answers."""
import json
import re

from backend.config.loader import Settings
from backend.llm.client import ClientPool, LLMError

JUDGE_PROMPT = """You are grading an IT-operations assistant's answer against ground truth.

Question: {query}

Ground-truth expected answer: {expected}

Grading criteria: {criteria}

Assistant's answer to grade:
---
{answer}
---

Score 1-5:
5 = correct AND comprehensive (covers the key facts with correct evidence)
4 = correct (right conclusion, minor omissions)
3 = partially correct (some right elements, but a key fact wrong or missing)
2 = mostly incorrect
1 = incorrect or unsupported

Respond with ONLY JSON: {{"score": <1-5>, "rationale": "<one sentence>"}}"""


async def evaluate(query: str, expected_answer: str, criteria: str, answer: str,
                   settings: Settings, pool: ClientPool) -> dict:
    prompt = JUDGE_PROMPT.format(query=query, expected=expected_answer,
                                 criteria=criteria, answer=answer)
    judge = settings.evaluator
    try:
        resp = await pool.get(judge.provider).chat(
            model=judge.name,
            messages=[{"role": "user", "content": prompt}],
            temperature=judge.params.get("temperature", 0),
            max_tokens=judge.params.get("max_tokens", 300),
        )
        m = re.search(r"\{.*\}", resp.content, re.DOTALL)
        data = json.loads(m.group(0)) if m else {}
        raw = data.get("score")
        if raw is None:
            raise ValueError("judge returned no score")
        score = int(float(raw))
        if not 1 <= score <= 5:
            raise ValueError(f"score out of range: {score}")
        return {"quality_score": score, "quality_rationale": data.get("rationale", ""),
                "judge_model": judge.name}
    except (LLMError, ValueError, TypeError, json.JSONDecodeError, AttributeError) as e:
        return {"quality_score": None, "quality_rationale": f"evaluation failed: {str(e)[:120]}",
                "judge_model": judge.name}
