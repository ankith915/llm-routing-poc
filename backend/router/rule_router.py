"""Strategy B: deterministic keyword/heuristic router.

SIMPLE -> cheap, MEDIUM -> medium, HARD -> premium.
"""
import re

HARD_PATTERNS = [
    r"root.?cause", r"\banaly[sz]", r"correlat", r"sequence of events", r"timeline",
    r"remediat", r"recommend", r"blast radius", r"architectur", r"determine whether",
    r"explain what caused", r"assess", r"prevent", r"mitigat",
]
MEDIUM_PATTERNS = [
    r"^why\b", r"\bwhy\b", r"compare", r"comparison", r"\brelated\b", r"relationship",
    r"pattern", r"contributing", r"affected", r"\bchanged\b", r"most errors",
    r"highest error", r"concern",
]

TIER_FOR_COMPLEXITY = {"SIMPLE": "cheap", "MEDIUM": "medium", "HARD": "premium"}


def classify(query: str) -> str:
    q = query.lower()
    if any(re.search(p, q) for p in HARD_PATTERNS):
        return "HARD"
    if any(re.search(p, q) for p in MEDIUM_PATTERNS) or len(q.split()) > 25:
        return "MEDIUM"
    return "SIMPLE"


def route(query: str) -> dict:
    complexity = classify(query)
    tier = TIER_FOR_COMPLEXITY[complexity]
    return {
        "strategy": "rule",
        "complexity": complexity,
        "recommended_tier": tier.upper(),
        "selected_tier": tier,
        "reason": f"Keyword heuristics classified the query as {complexity}.",
        "router_cost_usd": 0.0,
        "router_latency_ms": 0,
    }
