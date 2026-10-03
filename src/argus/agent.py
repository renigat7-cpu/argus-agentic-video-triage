"""The reasoning agent and its tools.

Three reasoners are provided and share one contract:

* :class:`HeuristicReasoner` - deterministic, zero-dependency, fully
  reproducible. This is the default so evaluation numbers are meaningful.
* :class:`LLMReasoner` - tool-calling model backend over an OpenAI-compatible
  endpoint.
* :class:`BedrockReasoner` (in :mod:`argus.bedrock`) - the same tool loop driven
  by the Bedrock Converse API.

Both model backends run the *same* bounded tool loop (:func:`run_tool_loop`):
the model chooses which of the typed tools to call, results are fed back, steps
are capped, every step is traced, and any failure falls back to the heuristic
reasoner so a decision is always produced.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Protocol

from .config import PolicyConfig
from .observability import ToolTrace
from .policy import Evidence, PolicyDecision, confidence_label, evaluate_evidence

DEFAULT_MAX_STEPS = 5


def max_steps(default: int = DEFAULT_MAX_STEPS) -> int:
    """``ARGUS_AGENT_MAX_STEPS``, clamped to at least one step."""

    raw = os.environ.get("ARGUS_AGENT_MAX_STEPS", "")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def fallback_reasoner(cfg: PolicyConfig) -> HeuristicReasoner:
    """The reasoner every backend degrades to."""

    return HeuristicReasoner(cfg)


class Reasoner(Protocol):
    """Contract every reasoner implements."""

    name: str

    def reason(self, evidence: Evidence, trace: ToolTrace) -> PolicyDecision: ...


@dataclass
class ToolRegistry:
    """Typed tools the agent is allowed to call.

    Tools never return raw video to a model. They return bounded numeric facts,
    which is what keeps the agent's input small and its cost predictable.
    """

    trace: ToolTrace

    def clip_summary(self, evidence: Evidence) -> dict[str, Any]:
        with self.trace.call("clip_summary", frames=len(evidence.frames)):
            return {
                "duration_s": round(evidence.clip_duration_s, 3),
                "fps": evidence.fps,
                "resolution": f"{evidence.width}x{evidence.height}",
                "location": evidence.location,
                "n_evidence_frames": len(evidence.frames),
            }

    def motion_profile(self, evidence: Evidence) -> dict[str, Any]:
        with self.trace.call("motion_profile"):
            return {
                "peak_motion": round(evidence.peak_motion, 4),
                "peak_flow": round(evidence.peak_flow, 4),
                "peak_saliency": round(evidence.peak_saliency, 4),
                "mean_luma": round(evidence.mean_luma, 4),
            }

    def timeline(self, evidence: Evidence) -> list[dict[str, Any]]:
        with self.trace.call("timeline", limit=PolicyConfig().max_evidence_frames):
            return [
                {
                    "t": f.timestamp_s,
                    "motion": round(f.motion, 4),
                    "flow": round(f.optical_flow_mag, 4),
                    "saliency": round(f.saliency, 4),
                }
                for f in evidence.frames[: PolicyConfig().max_evidence_frames]
            ]

    def gate_check(self, evidence: Evidence) -> dict[str, Any]:
        """Run the policy gates and report which ones failed."""

        with self.trace.call("gate_check"):
            decision = evaluate_evidence(evidence)
            return {
                "confidence": decision.confidence,
                "rationale": decision.rationale,
                "gates": [
                    {"rule": r.rule, "passed": r.passed, "confidence": r.confidence}
                    for r in decision.rules
                ],
            }


#: The complete tool surface. Model backends may not invent tools.
TOOL_NAMES: tuple[str, ...] = ("clip_summary", "motion_profile", "timeline", "gate_check")

TOOL_DESCRIPTIONS: dict[str, str] = {
    "clip_summary": "Container facts: duration, fps, resolution, location, frame count.",
    "motion_profile": "Peak motion, optical flow, saliency and mean luminance of the clip.",
    "timeline": "Per-frame motion/flow/saliency over the evidence timeline.",
    "gate_check": "Run the policy gates now and report which ones passed.",
}


def tool_functions_openai() -> list[dict[str, Any]]:
    """The four tools as OpenAI-compatible function definitions."""

    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": TOOL_DESCRIPTIONS[name],
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            },
        }
        for name in TOOL_NAMES
    ]


def tool_config_bedrock() -> dict[str, Any]:
    """The four tools as a Bedrock ``toolConfig`` block."""

    return {
        "tools": [
            {
                "toolSpec": {
                    "name": name,
                    "description": TOOL_DESCRIPTIONS[name],
                    "inputSchema": {"json": {"type": "object", "properties": {}}},
                }
            }
            for name in TOOL_NAMES
        ]
    }


@dataclass(frozen=True)
class ToolRequest:
    """A model-requested tool call, normalised across API dialects."""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


def dispatch_tool(
    registry: ToolRegistry, evidence: Evidence, request: ToolRequest
) -> dict[str, Any]:
    """Execute a requested tool. Failures are returned as data, never raised."""

    if request.name not in TOOL_NAMES:
        return {"error": f"unknown tool: {request.name}"}
    method = getattr(registry, request.name, None)
    if method is None:
        return {"error": f"unknown tool: {request.name}"}
    try:
        return {"result": method(evidence)}
    except Exception as exc:  # noqa: BLE001 — a tool failure is model-visible data
        return {"error": f"{type(exc).__name__}: {exc}"}


class ToolLoopBackend(Protocol):
    """Wire-format adapter: LLM chat messages and Bedrock messages are different."""

    def respond(self, messages: list[Any], step: int) -> tuple[Any, list[ToolRequest], str]: ...

    def tool_result(
        self, requests: list[ToolRequest], results: list[dict[str, Any]]
    ) -> list[Any]: ...


def run_tool_loop(
    evidence: Evidence,
    trace: ToolTrace,
    backend: ToolLoopBackend,
    max_iterations: int | None = None,
    initial: list[Any] | None = None,
) -> str:
    """Bounded model ⇄ tool loop shared by every model backend.

    The model is free to call any of the four typed tools, zero or more times;
    results are fed back until it produces text. Each iteration emits a
    ``<backend>_step`` trace call and each tool call is recorded by the registry.
    Raises when the model never returns usable text, so the caller can fall back.
    """

    registry = ToolRegistry(trace)
    messages: list[Any] = list(initial or [])
    steps = max_steps() if max_iterations is None else max(1, max_iterations)

    for step in range(1, steps + 1):
        assistant, requests, final_text = backend.respond(messages, step)
        if assistant is not None:
            messages.append(assistant)
        if not requests:
            if final_text.strip():
                return final_text
            raise RuntimeError(f"model produced no text after {step} step(s)")
        results = [dispatch_tool(registry, evidence, r) for r in requests]
        messages.extend(backend.tool_result(requests, results))

    raise RuntimeError(f"tool loop exceeded {steps} steps without a final answer")


def parse_final_json(text: str) -> dict[str, Any]:
    """Extract the ``{"confidence", "rationale"}`` object from model text."""

    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in model response")
    parsed = json.loads(text[start : end + 1])
    if not isinstance(parsed, dict) or "confidence" not in parsed:
        raise ValueError("model response has no confidence field")
    return parsed


def scored_from_model(evidence: Evidence, cfg: PolicyConfig, text: str) -> PolicyDecision:
    """Parse a model answer and route it through the shared confidence gates."""

    parsed = parse_final_json(text)
    confidence = max(0.0, min(1.0, float(parsed.get("confidence", 0.0))))
    scored = PolicyDecision(
        decision="candidate",
        confidence=round(confidence, 4),
        rules=evaluate_evidence(evidence).rules,
        rationale=str(parsed.get("rationale", ""))[:400],
    )
    return apply_gates(scored, cfg)


def fallback(
    evidence: Evidence, trace: ToolTrace, cfg: PolicyConfig, tag: str, exc: BaseException
) -> PolicyDecision:
    """Record why we degraded, then produce a heuristic decision anyway."""

    with trace.call(f"{tag}_fallback", reason=type(exc).__name__):
        print(f"argus.agent: {tag} failed, falling back to heuristic ({exc})", file=sys.stderr)
    return fallback_reasoner(cfg).reason(evidence, trace)


def apply_gates(decision: PolicyDecision, cfg: PolicyConfig) -> PolicyDecision:
    """Convert a scored candidate into confirm/dismiss/escalate.

    This is the only place the escalation boundary is defined, for every
    backend. A low-confidence confirm is the failure mode we care about most,
    so the boundary is checked here rather than inside each reasoner.
    """

    c = decision.confidence
    if c >= cfg.confirm_at:
        out = "confirm"
    elif c <= cfg.dismiss_at:
        out = "dismiss"
    else:
        out = "escalate"

    if cfg.require_motion_for_confirm and out == "confirm":
        if not any(r.rule == "motion_present" and r.passed for r in decision.rules):
            out = "escalate"

    rationale = decision.rationale
    if out == "escalate" and decision.decision != "escalate":
        rationale = (
            f"confidence {c:.3f} in escalation band "
            f"({cfg.dismiss_at} < c < {cfg.confirm_at}); {rationale}"
        )
    return PolicyDecision(
        decision=out,
        confidence=c,
        rules=decision.rules,
        rationale=rationale,
    )


class HeuristicReasoner:
    """Deterministic reasoner: no network, no model, fully reproducible."""

    name = "heuristic-v1"

    def __init__(self, cfg: PolicyConfig) -> None:
        self.cfg = cfg

    def reason(self, evidence: Evidence, trace: ToolTrace) -> PolicyDecision:
        tools = ToolRegistry(trace)
        tools.clip_summary(evidence)
        tools.motion_profile(evidence)
        tools.timeline(evidence)
        gate = tools.gate_check(evidence)
        scored = PolicyDecision(
            decision="candidate",
            confidence=float(gate["confidence"]),
            rules=evaluate_evidence(evidence).rules,
            rationale=str(gate["rationale"]),
        )
        return apply_gates(scored, self.cfg)


LLM_SYSTEM_PROMPT = """You are a video-triage reasoner for a security camera system.
You have tools that report bounded numeric facts about one evidence bundle.
Call the tools you need - clip_summary, motion_profile, timeline, gate_check -
then judge the evidence against these gates: motion_present, salient_subject,
illumination_usable, temporal_persistence.

Rules:
- Never invent observations that are not in the tool output.
- Return JSON only: {"confidence": <0..1>, "rationale": "<one sentence>"}.
- Confidence is your certainty that a real policy-relevant event is present.
- If the evidence is ambiguous, return a middling confidence. Do not round up;
  the system escalates to a human precisely when you are unsure."""


class LLMReasoner:
    """Tool-calling model backend over an OpenAI-compatible endpoint."""

    name = "llm-tools-v1"

    def __init__(self, cfg: PolicyConfig, model: str | None = None,
                 base_url: str | None = None, api_key_env: str = "ARGUS_LLM_KEY") -> None:
        self.cfg = cfg
        self.model = model or os.environ.get("ARGUS_LLM_MODEL", "gpt-4o-mini")
        self.base_url = base_url or os.environ.get("ARGUS_LLM_BASE_URL", "")
        self.api_key_env = api_key_env
        self.calls = 0

    def _endpoint(self) -> tuple[str, str]:
        key = os.environ.get(self.api_key_env, "")
        if not key:
            raise RuntimeError(f"{self.api_key_env} is not set")
        return (self.base_url or "https://api.openai.com/v1").rstrip("/"), key

    def reason(self, evidence: Evidence, trace: ToolTrace) -> PolicyDecision:
        tools = ToolRegistry(trace)
        try:
            facts = {
                "clip": tools.clip_summary(evidence),
                "motion": tools.motion_profile(evidence),
                "timeline": tools.timeline(evidence),
            }
        except Exception as exc:  # noqa: BLE001
            return fallback(evidence, trace, self.cfg, "llm", exc)

        prompt = (
            "Evidence bundle facts:\n"
            + json.dumps(facts, ensure_ascii=False)
            + "\n\nCall the tools you still need, then judge against the gates. "
            "Return JSON only."
        )
        backend = _OpenAIToolLoop(self, prompt, trace)
        try:
            text = run_tool_loop(evidence, trace, backend, initial=backend.initial())
        except Exception as exc:  # noqa: BLE001 — never fail a decision on the model
            return fallback(evidence, trace, self.cfg, "llm", exc)
        try:
            return scored_from_model(evidence, self.cfg, text)
        except Exception as exc:  # noqa: BLE001
            return fallback(evidence, trace, self.cfg, "llm", exc)


class _OpenAIToolLoop:
    """Adapts ``/chat/completions`` to :func:`run_tool_loop`."""

    def __init__(self, owner: LLMReasoner, prompt: str, trace: ToolTrace) -> None:
        self.owner = owner
        self.trace = trace
        self.system = {"role": "system", "content": LLM_SYSTEM_PROMPT}
        self.first = {"role": "user", "content": prompt}

    def initial(self) -> list[dict[str, Any]]:
        return [self.first]

    def respond(
        self, messages: list[dict[str, Any]], step: int
    ) -> tuple[Any, list[ToolRequest], str]:
        import httpx

        owner = self.owner
        base, key = owner._endpoint()
        with self.trace.call("llm_step", step=step, model=owner.model):
            resp = httpx.post(
                f"{base}/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json={
                    "model": owner.model,
                    "temperature": 0.0,
                    "max_tokens": 400,
                    "tools": tool_functions_openai(),
                    "tool_choice": "auto",
                    "messages": [self.system, *messages],
                },
                timeout=30.0,
            )
            resp.raise_for_status()
        owner.calls += 1
        message = (resp.json().get("choices") or [{}])[0].get("message", {})
        requests = [
            ToolRequest(
                id=str(c.get("id") or f"call_{step}_{i}"),
                name=str((c.get("function") or {}).get("name") or ""),
                arguments=(c.get("function") or {}).get("arguments") or {},
            )
            for i, c in enumerate(message.get("tool_calls") or [])
        ]
        assistant: dict[str, Any] = {"role": "assistant", "content": message.get("content") or ""}
        if requests:
            assistant["tool_calls"] = [
                {
                    "id": r.id,
                    "type": "function",
                    "function": {"name": r.name, "arguments": r.arguments},
                }
                for r in requests
            ]
        return assistant, requests, str(message.get("content") or "")

    def tool_result(
        self, requests: list[ToolRequest], results: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        # One tool message per call id, as the OpenAI wire format requires.
        return [
            {
                "role": "tool",
                "tool_call_id": r.id,
                "content": json.dumps(res, ensure_ascii=False, default=str),
            }
            for r, res in zip(requests, results, strict=False)
        ]


def build_reasoner(cfg: PolicyConfig, backend: str = "heuristic") -> Reasoner:
    """Factory so the backend is a config value rather than a code path."""

    name = (backend or "heuristic").strip().lower()
    if name == "llm":
        return LLMReasoner(cfg)
    if name == "bedrock":
        from .bedrock import BedrockReasoner

        return BedrockReasoner(cfg)
    if name != "heuristic":
        print(
            f"argus.agent: unknown reasoner backend {backend!r}, using heuristic-v1",
            file=sys.stderr,
        )
    return HeuristicReasoner(cfg)


__all__ = [
    "HeuristicReasoner",
    "LLMReasoner",
    "Reasoner",
    "TOOL_NAMES",
    "ToolRegistry",
    "ToolRequest",
    "apply_gates",
    "build_reasoner",
    "confidence_label",
    "dispatch_tool",
    "max_steps",
    "parse_final_json",
    "run_tool_loop",
    "scored_from_model",
    "tool_config_bedrock",
    "tool_functions_openai",
]
