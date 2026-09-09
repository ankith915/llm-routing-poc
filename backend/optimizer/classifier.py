"""Task classifier: the cheap rung of the decision ladder.

A multinomial naive-Bayes classifier over word unigrams and bigrams, trained
on the labelled query set and, optionally, on the LLM router's own logged
decisions (distillation: the expensive router teaches the free one). Pure
Python, deterministic, microseconds per query, no network.

It predicts two things independently from the same features:
  task_type   - which TaskType the request is
  difficulty  - SIMPLE | MEDIUM | HARD

Confidence is the posterior probability of the winning class. Below the
policy's `llm_router_confidence_threshold` the engine asks the LLM router
instead; the ledger records which rung answered, so the router's overhead is
never hidden.
"""
import math
import re
from collections import defaultdict
from dataclasses import dataclass, field

from backend import telemetry
from backend.optimizer import tasks

ROUTER_VERSION = "rules+lexical-nb-2"

# Tier 0: high-precision patterns. First match wins; the statistical model
# only decides when none applies. Ordered from most to least specific.
RULES = [
    ("alert_classification", r"\bclassif(y|ication)\b.*\b(alert|category|label)\b|\breply with (the )?(category|label)"),
    ("log_extraction", r"\b(extract|parse)\b.*\bjson\b|\bjson (object|only)\b|\bproduce a json\b"),
    ("sql_generation", r"\bsql\b|\bselect statement\b|\bwrite (a|one)? ?query\b"),
    ("ticket_summary", r"\bsummari[sz]e\b.*\btck-|\bticket\b.*\bsummar"),
    ("incident_summary", r"\b(status[- ]page|incident) (update|summary)\b|\bsummari[sz]e\b.*\binc-|\bwrite\b.*\bstatus[- ]page"),
    ("docs_qa", r"\b(runbook|playbook|policy|procedure)\b|\baccording to\b|\bper the\b"),
    ("timeline", r"\btimeline\b|\bsequence of events\b|\bchronolog"),
    ("remediation", r"\bremediat|\baction plan\b|\bprevent (recurrence|this)\b|\bmitigat|\barchitectur|\bfix(es)? (would|should|could)\b"),
    ("impact_assessment", r"\bblast radius\b|\buser impact\b|\bimpact of\b|\bwhich services were affected\b|\baffected by\b"),
    ("root_cause_analysis", r"\broot.?cause\b|\bwhat caused\b|\bcaused? the (outage|incident)\b|\boriginat(e|ed)\b|\bdid .* cause\b"),
    ("correlation", r"\bcorrelat|\brelationship\b|\brelated\b.*\?|\bare .* related\b"),
    ("comparison", r"\bcompar(e|ison)\b|\bversus\b|\bvs\.?\b|\bdifference between\b"),
    ("trend_analysis", r"\btrend\b|\bpattern\b|\bover the last\b|\bover time\b"),
    ("diagnosis", r"^\s*why\b|\bwhy is\b|\bwhat is causing\b|\bwhat changed\b|\breason for\b"),
    ("count_aggregate", r"\bhow many\b|\bcount\b|\bnumber of\b|\bthe most\b|\bhighest (error|number)"),
    ("health_check", r"\b(is|are)\b.*\b(healthy|up|down|ok|okay|running|reachable|alive)\b|\bwhich services? (is|are) (down|up|failing)\b|\bhealth\b"),
    ("incident_lookup", r"\b(severity|status|which service)\b.*\binc-\d+|\binc-\d+\b.*\b(severity|affect|status)\b|\bhow severe\b"),
    ("metric_lookup", r"\b(current|latest|right now|at the moment)\b.*\b(cpu|memory|latency|utili[sz]ation|usage|connections)\b|\b(cpu|memory|latency|usage)\b.*\b(right now|currently|now)\b|\bwhat is the (cpu|memory|latency)\b|\bhighest (memory|cpu|latency)\b"),
]
_COMPILED = [(t, re.compile(p, re.IGNORECASE)) for t, p in RULES]

# Labelled seed phrases per task type: a handful of ways people ask for each
# kind of work. They are training data, not test data.
SEEDS = {
    "metric_lookup": ["What is the current CPU of {svc}?", "Current memory utilization for {svc}", "How high is {svc} latency right now?", "Show me {svc} connection count", "What's the memory usage of {svc} at the moment?"],
    "health_check": ["Is {svc} healthy?", "Is {svc} up?", "Which services are down?", "Is {svc} currently reachable?", "Are any services unhealthy right now?"],
    "count_aggregate": ["How many errors did {svc} log?", "How many incidents are open?", "Which service produced the most errors?", "Count the WARN entries in the last hour", "Number of open HIGH incidents"],
    "incident_lookup": ["What is the severity of INC-1?", "Which service does INC-1 affect?", "What is the status of INC-1?", "Who owns incident INC-1?", "When was INC-1 created?"],
    "alert_classification": ["Classify this alert into one category: disk full", "Which category does this alert belong to?", "Label the alert: {svc} unreachable", "Categorize the following alert and reply with the label only"],
    "log_extraction": ["Extract the fields from this log line as JSON", "Parse this log entry into JSON with keys service, level", "Produce a JSON object for this incident record", "Return JSON only with the extracted fields"],
    "sql_generation": ["Write a SQL query that counts incidents by service", "Generate SQL to list ERROR logs per host", "SQL: latest metrics per service", "Write one SELECT statement against the logs table"],
    "docs_qa": ["According to the runbook, what should we do first?", "What does the on-call policy say about paging?", "Per the rollback procedure, who approves a rollback?", "What does RB-1 recommend?", "How long do we have to update the status page?"],
    "ticket_summary": ["Summarize ticket TCK-1 for the incident commander", "Give me a two-sentence summary of TCK-1", "What is TCK-1 asking for?", "Condense ticket TCK-1"],
    "incident_summary": ["Write a status-page update for INC-1", "Draft an incident summary for the exec channel", "Summarize INC-1 in three sentences", "Give a customer-facing summary of the outage"],
    "diagnosis": ["Why is {svc} slow?", "Why is {svc} throwing errors?", "What changed before {svc} degraded?", "What is causing the {svc} timeouts?", "Why did the retry queue grow?"],
    "comparison": ["Compare {svc} and {svc2} latency", "How does {svc} CPU compare with {svc2}?", "{svc} versus {svc2}: which is worse?", "Difference between {svc} and {svc2} error counts"],
    "trend_analysis": ["What pattern do you see in {svc} latency?", "Describe the trend in {svc} memory over the last hour", "How has {svc} CPU changed over time?"],
    "correlation": ["Is the database contributing to {svc} latency?", "Are the {svc} and {svc2} issues related?", "What is the relationship between connections and errors?", "Which incident is related to the checkout errors?"],
    "root_cause_analysis": ["What is the root cause of the {svc} incident?", "Analyze the outage and identify the root cause", "Did the deployment cause the outage?", "Where did the problem originate: app, database or network?", "Explain what caused the failure"],
    "timeline": ["Build a timeline of the cascading failure", "Give the sequence of events from first symptom to breaker open", "Reconstruct the chronology of the incident"],
    "remediation": ["Recommend remediation steps", "What would prevent recurrence?", "Give the incident commander an action plan", "What architectural changes would mitigate this?", "How do we fix this for good?"],
    "impact_assessment": ["Assess the blast radius of INC-1", "Which services were affected by the database issue?", "Quantify the user impact", "What is the impact of the outage on customers?"],
}
SEED_SLOTS = {"{svc}": "payment-api", "{svc2}": "checkout-web"}
_TOKEN = re.compile(r"[a-z][a-z0-9\-]+|\d+(?:\.\d+)?%?|inc-\d+|tck-\d+|rb-\d+")
_STOP = {"the", "a", "an", "of", "to", "is", "are", "in", "on", "and", "or", "for", "with",
         "it", "its", "this", "that", "be", "as", "at", "by", "from", "was", "were", "do", "does"}
DIFFICULTIES = ("SIMPLE", "MEDIUM", "HARD")


def features(text: str) -> list:
    words = [w for w in _TOKEN.findall(text.lower()) if w not in _STOP]
    words = [re.sub(r"\d+(?:\.\d+)?%?", "<num>", w) if w[0].isdigit() else w for w in words]
    # Normalise identifiers so INC-1001 and INC-1003 share a feature.
    words = [re.sub(r"(inc|tck|rb)-\d+", r"\1-<id>", w) for w in words]
    feats = list(words)
    feats += [f"{a}_{b}" for a, b in zip(words, words[1:])]
    # Shape features the bag misses: question form and length.
    first = words[0] if words else ""
    feats.append(f"<first:{first}>")
    n = len(words)
    feats.append("<len:short>" if n <= 8 else "<len:medium>" if n <= 20 else "<len:long>")
    if "?" not in text:
        feats.append("<imperative>")
    return feats


class NaiveBayes:
    def __init__(self, alpha: float = 0.5):
        self.alpha = alpha
        self.class_counts: dict = defaultdict(float)
        self.feature_counts: dict = defaultdict(lambda: defaultdict(float))
        self.feature_totals: dict = defaultdict(float)
        self.vocab: set = set()

    def fit_one(self, feats: list, label: str, weight: float = 1.0) -> None:
        self.class_counts[label] += weight
        for f in feats:
            self.feature_counts[label][f] += weight
            self.feature_totals[label] += weight
            self.vocab.add(f)

    def predict(self, feats: list) -> tuple:
        if not self.class_counts:
            return None, 0.0, {}
        total = sum(self.class_counts.values())
        v = len(self.vocab) or 1
        scores = {}
        for c, cc in self.class_counts.items():
            s = math.log(cc / total)
            denom = self.feature_totals[c] + self.alpha * v
            fc = self.feature_counts[c]
            for f in feats:
                s += math.log((fc.get(f, 0.0) + self.alpha) / denom)
            scores[c] = s
        mx = max(scores.values())
        probs = {c: math.exp(s - mx) for c, s in scores.items()}
        z = sum(probs.values())
        probs = {c: p / z for c, p in probs.items()}
        best = max(probs, key=probs.get)
        return best, probs[best], probs


@dataclass
class Classification:
    task_type: str
    difficulty: str
    confidence: float                 # min of the two posteriors
    task_confidence: float
    difficulty_confidence: float
    rung: str                         # rules | classifier | llm_router | hint
    required_capabilities: tuple
    alternatives: dict = field(default_factory=dict)
    note: str = ""

    def public(self) -> dict:
        return {"task_type": self.task_type, "task_label": tasks.get(self.task_type).label,
                "difficulty": self.difficulty, "confidence": round(self.confidence, 3),
                "task_confidence": round(self.task_confidence, 3),
                "difficulty_confidence": round(self.difficulty_confidence, 3),
                "rung": self.rung, "required_capabilities": list(self.required_capabilities),
                "alternatives": {k: round(v, 3) for k, v in sorted(
                    self.alternatives.items(), key=lambda kv: -kv[1])[:3]},
                "note": self.note, "router_version": ROUTER_VERSION}


class TaskClassifier:
    def __init__(self):
        self.task_model = NaiveBayes()
        self.difficulty_model = NaiveBayes()
        self.trained_on = {"labelled": 0, "logged": 0}

    # -------------------------------------------------------------- training
    def train_labelled(self, queries: list | None = None, seeds: bool = True) -> "TaskClassifier":
        for q in (queries or telemetry.load_test_queries()):
            f = features(q["query"])
            self.task_model.fit_one(f, q["task_type"])
            self.difficulty_model.fit_one(f, q["complexity"])
            self.trained_on["labelled"] += 1
        if seeds:
            for t, phrases in SEEDS.items():
                for ph in phrases:
                    for k, v in SEED_SLOTS.items():
                        ph = ph.replace(k, v)
                    f = features(ph)
                    self.task_model.fit_one(f, t, 0.7)
                    self.difficulty_model.fit_one(f, tasks.get(t).default_difficulty, 0.4)
        return self

    def train_logged(self, records: list, weight: float = 0.5) -> int:
        """Distil the LLM router: learn difficulty from its logged decisions on
        queries the labelled set does not cover."""
        known = {q["query"].strip().lower() for q in telemetry.load_test_queries()}
        n = 0
        for r in records:
            routing = r.get("routing") or {}
            cls = r.get("classification") or {}
            if cls.get("rung") != "llm_router" and routing.get("strategy") != "intelligent":
                continue
            diff = cls.get("difficulty") or routing.get("complexity")
            q = (r.get("query") or "").strip()
            if not q or q.lower() in known or diff not in DIFFICULTIES:
                continue
            f = features(q)
            self.difficulty_model.fit_one(f, diff, weight)
            if cls.get("task_type") in tasks.TASK_TYPES:
                self.task_model.fit_one(f, cls["task_type"], weight)
            n += 1
        self.trained_on["logged"] += n
        return n

    # ------------------------------------------------------------ inference
    def classify(self, query: str, hint: str | None = None) -> Classification:
        f = features(query)
        t, tp, talts = self.task_model.predict(f)
        d, dp, _ = self.difficulty_model.predict(f)
        rule = next((name for name, rx in _COMPILED if rx.search(query)), None)
        if hint and hint in tasks.TASK_TYPES:
            t, tp, rung, note = hint, 1.0, "hint", "task type supplied by the caller"
        elif rule:
            # A rule names the task; the model still owns difficulty unless it
            # agrees, in which case its posterior becomes the confidence.
            t, rung, note = rule, "rules", f"matched rule for {rule}"
            tp = max(tp if talts.get(rule) == tp else talts.get(rule, 0.0), 0.9)
        else:
            rung, note = "classifier", ""
        t = t or "diagnosis"
        d = d or tasks.get(t).default_difficulty
        # Rules on top of the statistics: difficulty can never sit below the
        # task type's floor (a remediation plan is never SIMPLE), and a
        # self-contained classification/extraction never needs HARD.
        floor = tasks.get(t).default_difficulty
        if tasks.DIFFICULTY_RANK[d] < tasks.DIFFICULTY_RANK[floor]:
            d, note = floor, f"difficulty raised to the task's floor ({floor})"
        if tasks.get(t).family in ("classification", "extraction") and d == "HARD":
            d, note = "MEDIUM", "self-contained task capped at MEDIUM"
        conf = min(tp, dp)
        return Classification(task_type=t, difficulty=d, confidence=conf, task_confidence=tp,
                              difficulty_confidence=dp, rung=rung,
                              required_capabilities=tasks.get(t).required_capabilities,
                              alternatives=talts, note=note)

    def leave_one_out_accuracy(self, queries: list | None = None) -> dict:
        """Honest self-assessment: train on all but one, predict the one."""
        qs = queries or telemetry.load_test_queries()
        ok_t = ok_d = 0
        for i, q in enumerate(qs):
            c = TaskClassifier().train_labelled(qs[:i] + qs[i + 1:]).classify(q["query"])
            ok_t += c.task_type == q["task_type"]
            ok_d += c.difficulty == q["complexity"]
        return {"n": len(qs), "task_type_accuracy": round(ok_t / len(qs), 3),
                "difficulty_accuracy": round(ok_d / len(qs), 3)}


_default: TaskClassifier | None = None


def get_classifier() -> TaskClassifier:
    global _default
    if _default is None:
        _default = TaskClassifier().train_labelled()
    return _default


def reset_classifier() -> None:
    global _default
    _default = None
