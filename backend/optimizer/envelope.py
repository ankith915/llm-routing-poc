"""RequestEnvelope: the one shape every entry point normalises into.

/api/query (dashboard), /v1/chat/completions (OpenAI-compatible) and the
experiment runner all build one of these; everything downstream reads only
the envelope.
"""
import uuid
from dataclasses import dataclass, field

STRATEGIES = ("none", "rule", "intelligent", "optimized")
MODES = ("live", "shadow", "live_internal")   # live_internal: executed, stored by the caller


@dataclass
class RequestEnvelope:
    query: str                                  # the user's question (last user message)
    tenant_id: str = "acme"
    application_id: str = "ops-assistant"
    environment: str = "production"
    feature_id: str | None = None
    session_id: str | None = None
    user_id: str | None = None
    messages: list = field(default_factory=list) # full conversation when supplied
    context: str | None = None                  # retrieved / telemetry context (built if None)
    task_type_hint: str | None = None
    sensitivity_class: str | None = None
    sla_class: str | None = None
    quality_target: float | None = None
    max_cost_usd: float | None = None
    output_schema: dict | None = None
    tools: list = field(default_factory=list)
    strategy: str = "optimized"                 # none | rule | intelligent | optimized | fixed:<model>
    mode: str = "live"                          # live | shadow
    evaluate: bool = False
    test_query: dict | None = None
    compression: str | None = None              # legacy experiment knob: off | headroom
    metadata: dict = field(default_factory=dict)
    request_id: str | None = None
    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def __post_init__(self):
        self.query = (self.query or "").strip()
        if not self.query and self.messages:
            self.query = next((m.get("content", "") for m in reversed(self.messages)
                               if m.get("role") == "user"), "").strip()
        if not self.query:
            raise ValueError("request has no user query")
        if self.mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        if not (self.strategy in STRATEGIES or self.strategy.startswith("fixed:")):
            raise ValueError(f"strategy must be one of {STRATEGIES} or fixed:<model_id>")
        self.tenant_id = (self.tenant_id or "acme").strip()
        self.application_id = (self.application_id or "ops-assistant").strip()

    def policy_overrides(self) -> dict:
        """Per-request policy layer: only the fields the caller actually set."""
        o = {}
        if self.sensitivity_class:
            o["sensitivity_class"] = self.sensitivity_class
        if self.sla_class:
            o["sla_class"] = self.sla_class
        if self.quality_target is not None:
            o["quality_target"] = float(self.quality_target)
        if self.max_cost_usd is not None:
            o["max_cost_per_request_usd"] = float(self.max_cost_usd)
        if self.feature_id:
            o["feature_id"] = self.feature_id
        return o

    @classmethod
    def from_openai(cls, body: dict, tenant_id: str, application_id: str, **kw) -> "RequestEnvelope":
        messages = list(body.get("messages") or [])
        meta = dict(body.get("metadata") or {})
        opt = dict(body.get("optimizer") or {})
        return cls(
            query="", messages=messages, tenant_id=tenant_id, application_id=application_id,
            environment=opt.get("environment", meta.get("environment", "production")),
            feature_id=opt.get("feature_id") or meta.get("feature_id"),
            session_id=opt.get("session_id") or meta.get("session_id"),
            user_id=body.get("user"),
            task_type_hint=opt.get("task_type"),
            sensitivity_class=opt.get("sensitivity_class"),
            sla_class=opt.get("sla_class"),
            quality_target=opt.get("quality_target"),
            max_cost_usd=opt.get("max_cost_usd"),
            output_schema=(body.get("response_format") or {}).get("json_schema"),
            tools=list(body.get("tools") or []),
            strategy=opt.get("strategy", "optimized"),
            mode=opt.get("mode", "live"),
            metadata={"requested_model": body.get("model"), **meta},
            **kw,
        )
