"""Quality gate: deterministic validators first, the LLM judge only when needed.

Every validator returns a `ValidationResult(passed, kind, detail, score)`.
`score` is on the judge's 1-5 scale so the gate can compare like with like:
a deterministic pass is 5, a deterministic failure is 1. Groundedness is
graded (fraction of cited numbers found in the context).

The gate combines: validator (if the task has one) -> judge (if ground truth
exists, or policy asks for it) and decides pass / escalate with a reason.
"""
import json
import re
import sqlite3
from dataclasses import dataclass, field

from backend import telemetry

try:
    import jsonschema
except Exception:                                   # pragma: no cover
    jsonschema = None


@dataclass
class ValidationResult:
    kind: str
    passed: bool
    score: float
    detail: str
    extracted: object = None

    def public(self) -> dict:
        return {"kind": self.kind, "passed": self.passed, "score": round(self.score, 2),
                "detail": self.detail}


# ------------------------------------------------------------------ helpers

def _strip_fences(text: str) -> str:
    t = text.strip()
    m = re.search(r"```(?:json|sql)?\s*(.*?)```", t, re.DOTALL | re.IGNORECASE)
    return m.group(1).strip() if m else t


def _first_json(text: str):
    t = _strip_fences(text)
    m = re.search(r"\{.*\}", t, re.DOTALL)
    if not m:
        raise ValueError("no JSON object in answer")
    return json.loads(m.group(0))


# --------------------------------------------------------------- validators

def validate_label(answer: str, allowed: list, expected: str | None = None) -> ValidationResult:
    a = re.sub(r"[^a-z0-9_\- ]", "", answer.strip().lower()).strip()
    a = a.split("\n")[0].strip()
    found = [l for l in allowed if l.lower() == a]
    if not found:
        # tolerate "Category: database" style answers, but only one label present
        present = [l for l in allowed if re.search(rf"\b{re.escape(l.lower())}\b", a)]
        found = present if len(present) == 1 else []
    if not found:
        return ValidationResult("label", False, 1.0, f"answer is not one of {allowed}: {answer[:60]!r}")
    if expected is not None and found[0].lower() != expected.lower():
        return ValidationResult("label", False, 1.0, f"label {found[0]!r} != expected {expected!r}", found[0])
    return ValidationResult("label", True, 5.0, f"label {found[0]!r}" + (" matches expected" if expected else ""), found[0])


def validate_json_schema(answer: str, schema: dict, expected: dict | None = None) -> ValidationResult:
    try:
        obj = _first_json(answer)
    except (ValueError, json.JSONDecodeError) as e:
        return ValidationResult("json_schema", False, 1.0, f"invalid JSON: {e}")
    if jsonschema is not None:
        try:
            jsonschema.validate(obj, schema)
        except jsonschema.ValidationError as e:
            return ValidationResult("json_schema", False, 1.5, f"schema violation: {e.message[:120]}", obj)
    else:                                                # pragma: no cover
        missing = [k for k in schema.get("required", []) if k not in obj]
        if missing:
            return ValidationResult("json_schema", False, 1.5, f"missing keys {missing}", obj)
    if expected:
        wrong = {k: (obj.get(k), v) for k, v in expected.items() if obj.get(k) != v}
        if wrong:
            return ValidationResult("json_schema", False, 2.0, f"schema ok but values differ: {wrong}", obj)
    return ValidationResult("json_schema", True, 5.0, "valid JSON matching schema" + (" and expected values" if expected else ""), obj)


def _demo_db() -> sqlite3.Connection:
    ds = telemetry.load_dataset()
    con = sqlite3.connect(":memory:")
    con.executescript(ds["schema"])
    con.executemany("insert into incidents values (?,?,?,?,?,?)",
                    [(i["incident_id"], i["service"], i["severity"], i["status"], i["description"], i["created_at"])
                     for i in ds["incidents"]])
    con.executemany("insert into logs values (?,?,?,?,?,?)",
                    [(l["timestamp"], l["service"], l["host"], l["level"], l["message"], l.get("latency_ms"))
                     for l in ds["logs"]])
    con.executemany("insert into metrics values (?,?,?,?,?,?,?)",
                    [(m["timestamp"], m["service"], m.get("cpu_percent"), m.get("memory_percent"),
                      m.get("latency_ms"), m.get("db_connections"), m.get("db_max_connections"))
                     for m in ds["metrics"]])
    return con


def _rows(con, sql: str, limit: int = 500) -> list:
    cur = con.execute(sql)
    return sorted(tuple(r) for r in cur.fetchmany(limit))


def validate_sql(answer: str, reference_sql: str) -> ValidationResult:
    sql = _strip_fences(answer).strip().rstrip(";")
    if not sql or not re.match(r"^\s*(select|with)\b", sql, re.IGNORECASE):
        return ValidationResult("sql", False, 1.0, "answer is not a SELECT statement")
    if re.search(r"\b(insert|update|delete|drop|alter|create|attach|pragma)\b", sql, re.IGNORECASE):
        return ValidationResult("sql", False, 1.0, "only read-only SELECT statements are accepted")
    con = _demo_db()
    try:
        got = _rows(con, sql)
    except sqlite3.Error as e:
        return ValidationResult("sql", False, 1.5, f"SQL failed to execute: {e}")
    want = _rows(con, reference_sql)
    # Compare as multisets of rows, ignoring column labels and ordering.
    norm = lambda rows: sorted(tuple(x for x in r) for r in rows)
    if norm(got) == norm(want):
        return ValidationResult("sql", True, 5.0, f"executed; {len(got)} rows match the reference result", sql)
    # Partial credit: same rows once extra columns are dropped (e.g. an added label column)
    if got and want and len(got) == len(want):
        proj = sorted(tuple(r[:len(want[0])]) for r in got)
        if proj == norm(want):
            return ValidationResult("sql", True, 4.5, "executed; rows match after dropping extra columns", sql)
    return ValidationResult("sql", False, 2.0, f"executed but result differs from reference ({len(got)} vs {len(want)} rows)", sql)


_NUM = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)(?:\s*(%|ms|s)\b)?")
_ENTITY = re.compile(r"\b(?:[a-z]+-(?:api|svc|web|lb-\d+|primary-\d+|replica-\d+)|server-\d+|db-\d+|net-\d+|INC-\d+|TCK-\d+|RB-\d+|POL-\d+)\b")


def validate_groundedness(answer: str, context: str) -> ValidationResult:
    """Do the numbers and entity names in the answer appear in the context?

    Times like 10:25 are matched as their components; percentages, latencies
    and counts must appear verbatim. A model that invents a number fails.
    """
    ctx = context or ""
    ctx_nums = set(m.group(1) for m in _NUM.finditer(ctx))
    ctx_nums |= set(re.findall(r"\d+", ctx))
    ans_nums = [m.group(1) for m in _NUM.finditer(answer)]
    ans_nums = [n for n in ans_nums if not re.fullmatch(r"[0-5]", n)]      # ordinals/list numbering
    ents = set(_ENTITY.findall(answer))
    ctx_ents = set(_ENTITY.findall(ctx))
    checks = len(ans_nums) + len(ents)
    if checks == 0:
        return ValidationResult("groundedness", True, 4.0, "no numbers or entities to verify")
    missing_nums = [n for n in ans_nums if n not in ctx_nums and n.rstrip("0").rstrip(".") not in ctx_nums]
    missing_ents = [e for e in ents if e not in ctx_ents]
    missing = len(missing_nums) + len(missing_ents)
    frac = 1 - missing / checks
    score = 1 + 4 * frac
    detail = f"{checks - missing}/{checks} cited values found in context"
    if missing:
        detail += f"; unsupported: {(missing_nums + missing_ents)[:4]}"
    return ValidationResult("groundedness", frac >= 0.75, round(score, 2), detail)


def validate_non_empty(answer: str) -> ValidationResult:
    ok = bool(answer and answer.strip())
    return ValidationResult("non_empty", ok, 5.0 if ok else 1.0, "answer present" if ok else "empty answer")


def run_validator(spec: dict | None, kind: str | None, answer: str, context: str) -> ValidationResult | None:
    """Run the deterministic validator a task/query asks for. spec (from the
    query) wins over kind (from the task type)."""
    kind = (spec or {}).get("type") or kind
    if not kind:
        return None
    if kind == "label":
        return validate_label(answer, spec.get("allowed", []), spec.get("expected")) if spec else None
    if kind == "json_schema":
        return validate_json_schema(answer, spec.get("schema", {}), spec.get("expected")) if spec else None
    if kind == "sql":
        return validate_sql(answer, spec["reference_sql"]) if spec and spec.get("reference_sql") else None
    if kind == "groundedness":
        return validate_groundedness(answer, context)
    return None


# ---------------------------------------------------------------------- gate

@dataclass
class GateResult:
    passed: bool
    score: float | None
    validators: list = field(default_factory=list)
    judge: dict | None = None
    reason: str = ""
    escalate: bool = False

    def public(self) -> dict:
        return {"passed": self.passed, "score": self.score,
                "validators": [v.public() for v in self.validators],
                "judge": self.judge, "reason": self.reason, "escalate": self.escalate}


def decide(validators: list, judge: dict | None, threshold: float, escalation_enabled: bool) -> GateResult:
    """Combine validator and judge outcomes into one verdict.

    A failed deterministic validator always fails the gate (it is a contract).
    Otherwise the judge score (if any) must clear the threshold. Groundedness
    below threshold fails on its own only when there is no judge.
    """
    hard = [v for v in validators if v.kind in ("label", "json_schema", "sql", "non_empty")]
    soft = [v for v in validators if v.kind == "groundedness"]
    judge_score = (judge or {}).get("quality_score")
    for v in hard:
        if not v.passed:
            return GateResult(False, v.score, validators, judge, f"{v.kind} validator failed: {v.detail}",
                              escalation_enabled)
    if judge_score is not None:
        if judge_score < threshold:
            return GateResult(False, float(judge_score), validators, judge,
                              f"judge scored {judge_score} < threshold {threshold}", escalation_enabled)
        return GateResult(True, float(judge_score), validators, judge, f"judge scored {judge_score} >= {threshold}")
    for v in soft:
        if not v.passed:
            return GateResult(False, v.score, validators, judge, f"groundedness: {v.detail}", escalation_enabled)
    if hard or soft:
        score = min(v.score for v in validators)
        return GateResult(True, score, validators, judge, "; ".join(v.detail for v in validators))
    return GateResult(True, None, validators, judge, "no validator applies; not graded")
