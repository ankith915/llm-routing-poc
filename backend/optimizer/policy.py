"""Policy engine: who may spend what, how, on which providers.

`resolve(tenant_id, application_id, overrides)` returns an `EffectivePolicy`
built by deep-merging defaults < tenant < application < overrides. Every
merged value is recorded with the layer it came from, so a trace can say
"quality_target 4.3 (application: finance-copilot)".

Feature flags follow the same idea with a different chain:
policy file < FLAG_<NAME> environment variable < runtime override.
"""
import copy
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
LAYERS = ("defaults", "tenant", "application", "request")

# Keys an application/tenant may set. Anything else in an override is ignored
# and reported, so a typo in a policy file is visible instead of silent.
POLICY_KEYS = {
    "environment", "sensitivity_class", "sla_class", "quality_target", "quality_gate_threshold",
    "latency_target_ms", "max_cost_per_request_usd", "frontier_allowed", "frontier_preferred",
    "allowed_providers", "allowed_models", "denied_models", "allow_unmeasured_models",
    "provider_sensitivity", "reasoning_policy", "output_budget_policy", "cache", "compression",
    "execution_mode_policy", "fallback", "escalation", "classifier", "budget", "prompt_version",
    "knowledge_version", "feature_id", "label",
}
SENSITIVITY_ORDER = ["public", "internal", "confidential", "restricted"]
SLA_CLASSES = ("interactive", "near_realtime", "batch", "offline")


def _deep_merge(base: dict, over: dict, provenance: dict, layer: str, prefix: str = "") -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        path = f"{prefix}{k}"
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v, provenance, layer, path + ".")
        else:
            out[k] = copy.deepcopy(v)
            provenance[path] = layer
    return out


@dataclass
class EffectivePolicy:
    tenant_id: str
    application_id: str
    values: dict
    provenance: dict            # dotted key -> layer that set it
    version: str
    flags: dict
    flag_provenance: dict
    ignored_keys: list = field(default_factory=list)

    def __getitem__(self, key):
        return self.values[key]

    def get(self, key, default=None):
        return self.values.get(key, default)

    def flag(self, name: str) -> bool:
        return bool(self.flags.get(name, False))

    def source(self, key: str) -> str:
        return self.provenance.get(key, "defaults")

    def explain(self, key: str) -> dict:
        return {"key": key, "value": self.values.get(key), "source": self.source(key)}

    def providers_for_sensitivity(self) -> list:
        cls = self.values.get("sensitivity_class", "internal")
        allowed = set(self.values.get("allowed_providers") or [])
        by_class = set((self.values.get("provider_sensitivity") or {}).get(cls, []))
        return sorted(allowed & by_class)

    def public(self) -> dict:
        return {"tenant_id": self.tenant_id, "application_id": self.application_id,
                "policy_version": self.version, "values": self.values,
                "provenance": self.provenance, "flags": self.flags,
                "flag_provenance": self.flag_provenance, "ignored_keys": self.ignored_keys}


class PolicyStore:
    def __init__(self, raw: dict):
        self.raw = raw
        self.version = str(raw.get("policy_version", "unversioned"))
        self.defaults = raw.get("defaults") or {}
        self.tenants = raw.get("tenants") or {}
        self.file_flags = {k: bool(v) for k, v in (raw.get("flags") or {}).items()}
        self.runtime_flags: dict = {}
        self.runtime_overrides: dict = {}   # (tenant, app) -> dict, set via API for demos

    # ----------------------------------------------------------------- flags
    def flags(self) -> tuple:
        flags, prov = {}, {}
        for k, v in self.file_flags.items():
            flags[k], prov[k] = v, "policy file"
        for k in list(flags):
            env = os.getenv(f"FLAG_{k}")
            if env is not None:
                flags[k], prov[k] = env.strip().lower() in ("1", "true", "yes", "on"), "environment"
        for k, v in self.runtime_flags.items():
            flags[k], prov[k] = bool(v), "runtime override"
        return flags, prov

    def set_flag(self, name: str, value: bool | None) -> None:
        if name not in self.file_flags:
            raise KeyError(f"unknown feature flag '{name}'")
        if value is None:
            self.runtime_flags.pop(name, None)
        else:
            self.runtime_flags[name] = bool(value)

    def set_override(self, tenant_id: str, application_id: str, values: dict | None) -> None:
        key = (tenant_id, application_id)
        if values is None:
            self.runtime_overrides.pop(key, None)
        else:
            self.runtime_overrides[key] = {**self.runtime_overrides.get(key, {}), **values}

    # --------------------------------------------------------------- resolve
    def tenant(self, tenant_id: str) -> dict:
        return self.tenants.get(tenant_id) or {}

    def application(self, tenant_id: str, application_id: str) -> dict:
        return (self.tenant(tenant_id).get("applications") or {}).get(application_id) or {}

    def known_tenant(self, tenant_id: str) -> bool:
        return tenant_id in self.tenants

    def resolve(self, tenant_id: str, application_id: str, overrides: dict | None = None) -> EffectivePolicy:
        provenance = {}
        ignored = []
        values = copy.deepcopy(self.defaults)
        for k in values:
            provenance[k] = "defaults"
        chain = [
            ("tenant", {k: v for k, v in self.tenant(tenant_id).items() if k != "applications"}),
            ("application", self.application(tenant_id, application_id)),
            ("runtime", self.runtime_overrides.get((tenant_id, application_id), {})),
            ("request", overrides or {}),
        ]
        for layer, doc in chain:
            clean = {}
            for k, v in (doc or {}).items():
                if k in POLICY_KEYS and v is not None:
                    clean[k] = v
                elif k not in POLICY_KEYS:
                    ignored.append(f"{layer}:{k}")
            values = _deep_merge(values, clean, provenance, layer)
        if values.get("sensitivity_class") not in SENSITIVITY_ORDER:
            raise ValueError(f"unknown sensitivity_class {values.get('sensitivity_class')!r}")
        if values.get("sla_class") not in SLA_CLASSES:
            raise ValueError(f"unknown sla_class {values.get('sla_class')!r}")
        flags, fprov = self.flags()
        return EffectivePolicy(tenant_id=tenant_id, application_id=application_id, values=values,
                               provenance=provenance, version=self.version, flags=flags,
                               flag_provenance=fprov, ignored_keys=ignored)

    def catalogue(self) -> dict:
        """Tenants and applications for the UI, with their effective policies."""
        out = {"policy_version": self.version, "defaults": self.defaults, "tenants": []}
        for tid, t in self.tenants.items():
            apps = []
            for aid, a in (t.get("applications") or {}).items():
                eff = self.resolve(tid, aid)
                apps.append({"id": aid, "label": a.get("label", aid), "policy": eff.values,
                             "provenance": eff.provenance})
            out["tenants"].append({"id": tid, "label": t.get("label", tid),
                                   "budget": t.get("budget"), "applications": apps})
        flags, prov = self.flags()
        out["flags"] = {k: {"value": v, "source": prov[k]} for k, v in flags.items()}
        return out


def load_policies(path: Path | None = None) -> PolicyStore:
    path = path or (CONFIG_DIR / "policies.yaml")
    return PolicyStore(yaml.safe_load(path.read_text()))


_store: PolicyStore | None = None


def get_policies() -> PolicyStore:
    global _store
    if _store is None:
        _store = load_policies()
    return _store


def reset_policies() -> None:
    global _store
    _store = None
