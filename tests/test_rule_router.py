from backend.router import rule_router


def test_simple_queries_go_cheap():
    for q in ["What is the current CPU usage of payment-api?",
              "Is payment-api currently healthy?",
              "How many incidents are currently open?"]:
        r = rule_router.route(q)
        assert r["complexity"] == "SIMPLE"
        assert r["selected_tier"] == "cheap"


def test_medium_queries_go_medium():
    for q in ["Why is payment-api experiencing high latency?",
              "Compare the current CPU usage of payment-api and checkout-web.",
              "What changed shortly before the increase in payment-api CPU usage?"]:
        r = rule_router.route(q)
        assert r["complexity"] == "MEDIUM"
        assert r["selected_tier"] == "medium"


def test_hard_queries_go_premium():
    for q in ["Analyze the payment-api incident and identify the most likely root cause.",
              "Correlate the CPU, latency and database timeout data and explain what caused the outage.",
              "Given the available logs and metrics, identify the root cause and recommend remediation steps."]:
        r = rule_router.route(q)
        assert r["complexity"] == "HARD"
        assert r["selected_tier"] == "premium"


def test_router_reports_zero_overhead():
    r = rule_router.route("Is auth-svc healthy?")
    assert r["router_cost_usd"] == 0.0
    assert r["router_latency_ms"] == 0
