import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import storage                                    # noqa: E402
from backend.llm.client import ClientPool, LLMError, LLMResponse   # noqa: E402
from backend.optimizer import classifier as classifier_mod      # noqa: E402
from backend.optimizer import health as health_mod              # noqa: E402
from backend.optimizer import policy as policy_mod              # noqa: E402
from backend.optimizer import registry as registry_mod          # noqa: E402


class FakeClient:
    """One fake provider that answers routing, judging and answering prompts.

    Behaviour is steerable per test:
      failing_models  -> raise LLMError for these model ids
      blank_models    -> return an empty completion (the gpt-oss truncation mode)
      answers         -> {model_id or "*": text} to control the answer content
      judge_scores    -> [scores] handed out in order, or a single int
    """

    def __init__(self, failing_models=(), blank_models=(), answers=None, judge_scores=4,
                 router_decision=None, cached_input_tokens=0, reasoning_tokens=0, latency_ms=900):
        self.failing = set(failing_models)
        self.blank = set(blank_models)
        self.answers = answers or {}
        self.judge_scores = judge_scores if isinstance(judge_scores, list) else None
        self.judge_score = judge_scores if not isinstance(judge_scores, list) else None
        self.router_decision = router_decision
        self.cached_input_tokens = cached_input_tokens
        self.reasoning_tokens = reasoning_tokens
        self.latency_ms = latency_ms
        self.calls = []
        self.answer_calls = []
        self.embed_calls = 0
        self.provider = "fake"
        self.chaos = None

    def as_pool(self, providers=("groq", "openai", "openrouter")):
        return ClientPool({p: self for p in providers})

    def has(self, provider):        # so a bare client can stand in for a pool
        return True

    def get(self, provider):
        return self

    async def chat(self, model, messages, temperature=0.2, max_tokens=900, retries=2,
                   extra_params=None):
        text = messages[-1]["content"]
        self.calls.append(model)
        if "routing controller" in text:
            d = self.router_decision or {"task_type": "diagnosis", "difficulty": "MEDIUM",
                                         "confidence": 0.9, "reason": "needs light correlation"}
            return LLMResponse(json.dumps(d), model, "fake", 200, 60, 150)
        if "grading an IT-operations assistant" in text:
            score = self.judge_scores.pop(0) if self.judge_scores else self.judge_score
            return LLMResponse(json.dumps({"score": score, "rationale": "graded"}), model, "fake", 300, 30, 120)
        if model in self.failing:
            raise LLMError(f"simulated outage for {model}")
        self.answer_calls.append({"model": model, "messages": messages, "max_tokens": max_tokens,
                                  "params": dict(extra_params or {})})
        if model in self.blank:
            return LLMResponse("   ", model, "fake", 1000, 5, self.latency_ms)
        content = self.answers.get(model, self.answers.get("*", "The DB connection pool is exhausted."))
        # input tokens track the prompt length so context savings show up in cost
        in_tok = max(1, len(text) // 4)
        return LLMResponse(content, model, "fake", in_tok, max(1, len(content) // 4), self.latency_ms,
                           cached_input_tokens=self.cached_input_tokens,
                           reasoning_tokens=self.reasoning_tokens, finish_reason="stop")

    # A stand-in for a real embedding model: canonicalise synonyms and drop
    # function words before hashing, so true paraphrases land close together
    # the way sentence embeddings do. The local bag-of-words embedder in
    # embeddings.py deliberately does not do this - that difference is the
    # point of cache.measure_separation.
    SYNONYMS = {"utilization": "usage", "utilisation": "usage", "consumption": "usage",
                "what's": "what", "whats": "what", "right": "", "now": "current",
                "currently": "current", "moment": "current", "at": "", "the": "", "is": "",
                "of": "", "on": "", "a": "", "an": "", "in": "", "for": "", "to": "",
                "service": "svc", "auth": "auth-svc", "auth-svc": "auth-svc",
                "severity": "severe", "severe": "severe", "how": "what",
                "experiencing": "having", "causing": "cause", "cause": "cause",
                "high": "high", "incident": ""}

    async def aclose(self):
        return None

    async def embed(self, model, text):
        self.embed_calls += 1
        from backend.optimizer.embeddings import local_vector, _TOKEN
        words = [self.SYNONYMS.get(w, w) for w in _TOKEN.findall(text.lower())]
        canon = " ".join(sorted(w for w in words if w))
        return {"vector": local_vector(canon), "tokens": max(1, len(text) // 4), "latency_ms": 5}


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Isolated flat-file store plus fresh optimizer singletons."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    storage._reset_for_tests(tmp_path / "results.json")
    registry_mod.reset_registry()
    policy_mod.reset_policies()
    classifier_mod.reset_classifier()
    health_mod.reset_health()
    from backend import pipeline
    pipeline.invalidate_learned()
    yield storage
    storage._reset_for_tests(Path(__file__).resolve().parent.parent / "backend" / "data" / "results.json")


@pytest.fixture
def settings():
    from backend.config.loader import load_settings
    return load_settings()


@pytest.fixture(autouse=True)
def _no_real_providers(monkeypatch, request):
    """Belt and braces: no test may build a real provider client.

    The FastAPI lifespan constructs a ClientPool from .env, so without this a
    test that starts the app would quietly spend real provider credit.
    """
    if "allow_network" in request.keywords:
        return
    from backend.llm import client as client_mod

    def forbidden(*a, **kw):
        raise AssertionError("a test tried to construct a real LLM client - tests run offline")

    monkeypatch.setattr(client_mod.LLMClient, "__init__", forbidden)
