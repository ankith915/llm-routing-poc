import json
from pathlib import Path

from backend import telemetry

DATA = Path(__file__).resolve().parent.parent / "backend" / "data"


def test_dataset_files_are_valid_json():
    for name in ("logs.json", "metrics.json", "incidents.json", "test_queries.json"):
        json.loads((DATA / name).read_text())


def test_error_count_matches_expected_answer():
    logs = json.loads((DATA / "logs.json").read_text())
    assert sum(1 for l in logs if l["level"] == "ERROR") == 12


def test_test_query_set_size_and_shape():
    qs = telemetry.load_test_queries()
    assert 30 <= len(qs) <= 80
    for q in qs:
        assert q["complexity"] in ("SIMPLE", "MEDIUM", "HARD")
        assert q["expected_answer"] and q["criteria"] and q["id"]


def test_context_is_identical_across_strategies():
    q = "Why is payment-api experiencing high latency?"
    assert telemetry.build_context(q) == telemetry.build_context(q)


def test_context_includes_logs_for_analysis_queries():
    ctx = telemetry.build_context("Analyze the payment-api incident and identify the root cause.")
    assert "Log stream" in ctx and "Circuit breaker opened" in ctx


def test_context_always_has_metrics_and_incidents():
    ctx = telemetry.build_context("What is the current CPU usage of payment-api?")
    assert "cpu_percent=87" in ctx and "INC-1001" in ctx
