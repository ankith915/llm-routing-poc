"""Task classifier ladder and deterministic validators."""
import json

import pytest

from backend import telemetry
from backend.optimizer import quality, tasks
from backend.optimizer.classifier import TaskClassifier, features, get_classifier


@pytest.fixture(scope="module")
def clf():
    return get_classifier()


def test_features_normalise_ids_and_numbers():
    f = features("What is the severity of INC-1001? It is 87% now")
    assert "inc-<id>" in f and "<num>" in f
    assert "<first:what>" in f


def test_rules_take_precedence_and_report_rung(clf):
    c = clf.classify("Classify this alert into one category from [database, network]: disk full")
    assert c.task_type == "alert_classification" and c.rung == "rules" and c.difficulty == "SIMPLE"
    c = clf.classify("Write one SQL query that counts incidents per service")
    assert c.task_type == "sql_generation"
    assert c.required_capabilities == ()


def test_hint_wins_over_everything(clf):
    c = clf.classify("Why is payment-api slow?", hint="ticket_summary")
    assert c.task_type == "ticket_summary" and c.rung == "hint" and c.task_confidence == 1.0


def test_difficulty_floor_from_task_type(clf):
    c = clf.classify("Recommend remediation steps for the DB")
    assert c.task_type == "remediation" and c.difficulty == "HARD"


def test_low_confidence_for_gibberish(clf):
    c = clf.classify("hello")
    assert c.confidence < 0.55


def test_leave_one_out_accuracy_is_measured_and_decent(clf):
    acc = clf.leave_one_out_accuracy()
    assert acc["n"] == len(telemetry.load_test_queries())
    assert acc["task_type_accuracy"] >= 0.85
    assert acc["difficulty_accuracy"] >= 0.75


def test_train_logged_distils_llm_router_decisions():
    c = TaskClassifier().train_labelled()
    records = [{"query": "Please investigate the weird spike thing on the cluster",
                "routing": {"strategy": "intelligent", "complexity": "HARD"},
                "classification": {"rung": "llm_router", "task_type": "root_cause_analysis", "difficulty": "HARD"}},
               {"query": "What is the current CPU usage of payment-api?",   # already labelled -> skipped
                "routing": {"strategy": "intelligent", "complexity": "SIMPLE"}},
               {"query": "x", "routing": {"strategy": "rule", "complexity": "SIMPLE"}}]   # not an LLM decision
    assert c.train_logged(records) == 1
    assert c.trained_on["logged"] == 1


def test_every_task_type_has_a_seed_or_rule():
    from backend.optimizer.classifier import RULES, SEEDS
    covered = {t for t, _ in RULES} | set(SEEDS)
    assert set(tasks.TASK_TYPES) <= covered


# ---------------------------------------------------------------- validators

LABELS = ["database", "network", "application", "capacity", "security"]


def test_label_validator():
    assert quality.validate_label("database", LABELS, "database").passed
    assert quality.validate_label("Category: Database.", LABELS, "database").passed
    r = quality.validate_label("It is probably the database or the network", LABELS, "database")
    assert not r.passed and "not one of" in r.detail
    r = quality.validate_label("network", LABELS, "database")
    assert not r.passed and "!= expected" in r.detail and r.score == 1.0


SCHEMA = {"type": "object", "required": ["service", "level", "latency_ms"],
          "properties": {"service": {"type": "string"}, "level": {"enum": ["INFO", "WARN", "ERROR"]},
                         "latency_ms": {"type": ["integer", "null"]}}}


def test_json_schema_validator():
    good = '```json\n{"service": "payment-api", "level": "ERROR", "latency_ms": 5230}\n```'
    assert quality.validate_json_schema(good, SCHEMA, {"service": "payment-api", "latency_ms": 5230}).passed
    bad = '{"service": "payment-api", "level": "FATAL", "latency_ms": 5230}'
    r = quality.validate_json_schema(bad, SCHEMA)
    assert not r.passed and "schema violation" in r.detail
    r = quality.validate_json_schema('{"service": "x", "level": "ERROR", "latency_ms": 1}', SCHEMA, {"latency_ms": 5230})
    assert not r.passed and "values differ" in r.detail
    assert not quality.validate_json_schema("no json here", SCHEMA).passed


def test_sql_validator_executes_against_demo_data():
    ref = "SELECT severity, COUNT(*) FROM incidents WHERE status='OPEN' GROUP BY severity"
    ok = quality.validate_sql("```sql\nSELECT severity, COUNT(*) AS n FROM incidents WHERE status = 'OPEN' GROUP BY severity;\n```", ref)
    assert ok.passed and ok.score == 5.0
    wrong = quality.validate_sql("SELECT severity, COUNT(*) FROM incidents GROUP BY severity", ref)
    assert not wrong.passed and "differs" in wrong.detail
    broken = quality.validate_sql("SELECT nope FROM incidents", ref)
    assert not broken.passed and "failed to execute" in broken.detail
    assert not quality.validate_sql("DROP TABLE incidents", ref).passed
    assert not quality.validate_sql("I would run a query", ref).passed


def test_sql_validator_partial_credit_for_extra_columns():
    ref = "SELECT service, COUNT(*) FROM logs WHERE level='ERROR' GROUP BY service"
    r = quality.validate_sql("SELECT service, COUNT(*) AS errors, MAX(ts) FROM logs WHERE level='ERROR' GROUP BY service", ref)
    assert r.passed and r.score == 4.5


def test_groundedness_validator():
    ctx = telemetry.build_context("What is the current CPU usage of payment-api?")
    ok = quality.validate_groundedness("payment-api CPU is 87% as of 10:25.", ctx)
    assert ok.passed and ok.score >= 4.5
    bad = quality.validate_groundedness("payment-api CPU is 93% and search-svc is at 77%, see INC-9999.", ctx)
    assert not bad.passed and "unsupported" in bad.detail
    none = quality.validate_groundedness("Everything looks fine.", ctx)
    assert none.passed and none.score == 4.0


def test_run_validator_dispatch():
    ctx = "cpu_percent=87"
    tq = next(t for t in telemetry.load_test_queries() if t["id"] == "Q-37")
    r = quality.run_validator(tq["validator"], "label", "database", ctx)
    assert r.kind == "label" and r.passed
    assert quality.run_validator(None, "groundedness", "87% cpu", ctx).passed
    assert quality.run_validator(None, None, "x", ctx) is None
    assert quality.run_validator(None, "sql", "select 1", ctx) is None   # needs a reference


def test_gate_decision_rules():
    v_fail = quality.ValidationResult("label", False, 1.0, "wrong label")
    v_ok = quality.ValidationResult("label", True, 5.0, "ok")
    g_soft = quality.ValidationResult("groundedness", False, 2.0, "invented numbers")
    # hard validator failure beats a good judge score
    g = quality.decide([v_fail], {"quality_score": 5}, 3.5, True)
    assert not g.passed and g.escalate and "label validator failed" in g.reason
    # judge below threshold fails
    g = quality.decide([v_ok], {"quality_score": 3}, 3.5, False)
    assert not g.passed and not g.escalate
    # judge above threshold passes even if groundedness heuristic complained
    g = quality.decide([g_soft], {"quality_score": 4}, 3.5, True)
    assert g.passed and g.score == 4.0
    # no judge: groundedness failure fails
    g = quality.decide([g_soft], None, 3.5, True)
    assert not g.passed and "groundedness" in g.reason
    # nothing to check
    g = quality.decide([], None, 3.5, True)
    assert g.passed and g.score is None


# ------------------------------------------------------------ execution plan

def test_output_budget_leaves_room_for_hidden_reasoning():
    """Regression: a 20-token budget on a reasoning model produced an empty
    completion, which the pipeline treats as a failure and fails over to a more
    expensive model - costing more than not capping the output at all."""
    from backend.optimizer import planner
    from backend.optimizer.policy import load_policies
    from backend.optimizer.registry import get_registry
    reg = get_registry()
    pol = load_policies().resolve("acme", "ops-assistant")
    cheap = reg.tier_default("cheap")          # gpt-oss-20b: reasoning controls
    p = planner.plan(cheap, "alert_classification", "SIMPLE", pol, reg)
    measured_low = cheap.reasoning_tokens_measured["low"]
    assert p.output_budget_tokens > 20 + measured_low, (
        "the budget must cover the visible answer and the model's measured hidden reasoning")
    assert any("hidden reasoning" in r for r in p.reasons)
    # a model without reasoning controls needs no headroom
    premium = reg.tier_default("premium")
    p2 = planner.plan(premium, "alert_classification", "SIMPLE", pol, reg)
    assert p2.output_budget_tokens == 20


def test_output_budget_never_exceeds_the_configured_ceiling():
    from backend.optimizer import planner
    from backend.optimizer.policy import load_policies
    from backend.optimizer.registry import get_registry
    reg = get_registry()
    pol = load_policies().resolve("acme", "ops-assistant")
    p = planner.plan(reg.tier_default("cheap"), "remediation", "HARD", pol, reg,
                     answer_max_tokens=500)
    assert p.output_budget_tokens <= 500
