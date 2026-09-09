"""Context optimizer: send the model only what the task needs.

Transforms, applied in order, each recorded with before/after tokens:
  section_selection  keep only the corpus sections the task type reads
                     (a label classification does not need the log stream)
  dedupe_lines       drop repeated lines inside a section
  whitespace         collapse runs of blank lines
  compression        Headroom fold (only when installed and the tier policy
                     allows it for the selected model's tier - the measured
                     finding is that the cheap tier misreads the fold)

Prompt-cache layout: the returned messages put the stable system prompt and
context block first and the question last, so provider prefix caching can
reuse the static part. The boundary is reported so the trace can show it.
"""
import re
from dataclasses import dataclass, field

from backend import compression, telemetry
from backend.config.loader import TIER_RANK
from backend.optimizer import tasks

_BLANKS = re.compile(r"\n{3,}")


@dataclass
class Transform:
    name: str
    tokens_before: int
    tokens_after: int
    detail: str = ""
    applied: bool = True

    @property
    def saved(self) -> int:
        return self.tokens_before - self.tokens_after

    def public(self) -> dict:
        return {"name": self.name, "tokens_before": self.tokens_before, "tokens_after": self.tokens_after,
                "tokens_saved": self.saved, "detail": self.detail, "applied": self.applied}


@dataclass
class ContextPlan:
    context: str
    baseline_context: str
    tokens_before: int
    tokens_after: int
    transforms: list = field(default_factory=list)
    sections_kept: list = field(default_factory=list)
    sections_dropped: list = field(default_factory=list)
    static_tokens: int = 0            # system prompt + context (cacheable prefix)
    dynamic_tokens: int = 0           # the question
    compression_mode: str = "off"
    compression_error: str | None = None

    @property
    def tokens_saved(self) -> int:
        return self.tokens_before - self.tokens_after

    @property
    def saved_pct(self) -> float:
        return round(100 * self.tokens_saved / self.tokens_before, 1) if self.tokens_before else 0.0

    def public(self) -> dict:
        return {"tokens_before": self.tokens_before, "tokens_after": self.tokens_after,
                "tokens_saved": self.tokens_saved, "saved_pct": self.saved_pct,
                "transforms": [t.public() for t in self.transforms],
                "sections_kept": self.sections_kept, "sections_dropped": self.sections_dropped,
                "static_tokens": self.static_tokens, "dynamic_tokens": self.dynamic_tokens,
                "compression_mode": self.compression_mode, "compression_error": self.compression_error}


def count(text: str) -> int:
    return compression.count_tokens(text)


def messages_for(context: str, query: str) -> list:
    """Static first, dynamic last - the shape prefix caching rewards."""
    return [
        {"role": "system", "content": telemetry.SYSTEM_PROMPT},
        {"role": "user", "content": f"Context:\n{context}\n\nQuestion: {query}"},
    ]


def compression_allowed(policy_compression: dict, model_tier: str) -> tuple:
    mode = (policy_compression or {}).get("mode", "tier_aware")
    if mode == "off":
        return False, "compression policy: off"
    if mode == "always":
        return True, "compression policy: always"
    min_tier = (policy_compression or {}).get("min_tier", "medium")
    ok = TIER_RANK.get(model_tier, 0) >= TIER_RANK.get(min_tier, 2)
    return ok, (f"tier-aware policy: {model_tier} tier {'>=' if ok else '<'} {min_tier}; "
                + ("compress" if ok else "send original (measured: cheap tier misreads folded context)"))


def optimise(query: str, task_type: str, *, enabled: bool = True, policy_compression: dict | None = None,
             model_tier: str = "premium", legacy_compression: str | None = None,
             supplied_context: str | None = None) -> ContextPlan:
    """Build the baseline context, then shrink it for the task and tier."""
    if supplied_context is not None:
        # Caller brought its own context (OpenAI endpoint): only generic transforms apply.
        base = supplied_context
        sections = {"supplied": base}
        keep_names = ["supplied"]
        baseline_names = ["supplied"]
    else:
        sections = telemetry.build_sections(query)
        baseline_names = telemetry.baseline_section_names(query)
        base = telemetry.assemble(sections, baseline_names)
        keep_names = list(baseline_names)
    before = count(base)
    plan = ContextPlan(context=base, baseline_context=base, tokens_before=before, tokens_after=before,
                       sections_kept=list(keep_names), sections_dropped=[])
    if not enabled:
        plan.transforms.append(Transform("disabled", before, before,
                                         "context optimisation off for this strategy", False))
        # The published compression A/B runs every strategy - including the
        # baseline - with and without the compressor, so an explicit
        # compression mode still applies when the rest of the stage is off.
        if legacy_compression == "headroom":
            out_ctx, st = compression.compress_context(plan.context, query, "headroom")
            plan.compression_mode = "headroom"
            plan.compression_error = st["error"]
            plan.transforms.append(Transform("compression", plan.tokens_after, st["tokens_after"],
                                             f"headroom: {', '.join(st['transforms']) or 'no transform'}"
                                             + (f"; error: {st['error']}" if st["error"] else ""),
                                             applied=st["error"] is None))
            plan.context, plan.tokens_after = out_ctx, st["tokens_after"]
    else:
        # 1. section selection by task type (only for corpus-built context)
        if supplied_context is None:
            task = tasks.get(task_type)
            wanted = set(task.context_sections)
            # never drop a section the query names explicitly (e.g. 'runbook', 'ticket')
            explicit = set(baseline_names) - {"metrics", "incidents", "logs"}
            keep = [s for s in baseline_names if s in wanted or s in explicit]
            if not keep and baseline_names:
                keep = baseline_names[:1]
            if keep != baseline_names:
                ctx = telemetry.assemble(sections, keep)
                after = count(ctx)
                dropped = [s for s in baseline_names if s not in keep]
                plan.transforms.append(Transform("section_selection", plan.tokens_after, after,
                                                 f"task {task.name} reads {keep}; dropped {dropped}"))
                plan.context, plan.tokens_after = ctx, after
                plan.sections_kept, plan.sections_dropped = keep, dropped
            else:
                plan.transforms.append(Transform("section_selection", plan.tokens_after, plan.tokens_after,
                                                 f"task {task.name} needs every section already sent", False))
        # 2. dedupe lines
        lines, seen, out = plan.context.split("\n"), set(), []
        for ln in lines:
            key = ln.strip()
            if key and key in seen and not key.startswith("#"):
                continue
            seen.add(key)
            out.append(ln)
        ctx = "\n".join(out)
        if ctx != plan.context:
            after = count(ctx)
            plan.transforms.append(Transform("dedupe_lines", plan.tokens_after, after,
                                             f"removed {len(lines) - len(out)} duplicate lines"))
            plan.context, plan.tokens_after = ctx, after
        # 3. whitespace
        ctx = _BLANKS.sub("\n\n", plan.context).strip()
        if ctx != plan.context:
            after = count(ctx)
            plan.transforms.append(Transform("whitespace", plan.tokens_after, after, "collapsed blank runs"))
            plan.context, plan.tokens_after = ctx, after
        # 4. compression (Headroom), tier-aware
        allowed, why = compression_allowed(policy_compression, model_tier)
        if legacy_compression == "off":
            plan.transforms.append(Transform("compression", plan.tokens_after, plan.tokens_after,
                                             "compression off for this run", False))
        elif legacy_compression == "headroom" or (allowed and compression.available()):
            out_ctx, st = compression.compress_context(plan.context, query, "headroom")
            plan.compression_mode = "headroom"
            plan.compression_error = st["error"]
            after = st["tokens_after"]
            detail = f"headroom: {', '.join(st['transforms']) or 'no transform'}; {why}"
            if st["error"]:
                detail += f"; error: {st['error']}"
            plan.transforms.append(Transform("compression", plan.tokens_after, after, detail,
                                             applied=st["error"] is None and after < plan.tokens_after))
            plan.context, plan.tokens_after = out_ctx, after
        elif allowed:
            plan.transforms.append(Transform("compression", plan.tokens_after, plan.tokens_after,
                                             f"{why}; headroom-ai not installed on this host", False))
        else:
            plan.transforms.append(Transform("compression", plan.tokens_after, plan.tokens_after, why, False))
    msgs = messages_for(plan.context, query)
    plan.static_tokens = count(msgs[0]["content"]) + count(f"Context:\n{plan.context}")
    plan.dynamic_tokens = count(f"\n\nQuestion: {query}")
    return plan
