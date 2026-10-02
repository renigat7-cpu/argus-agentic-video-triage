"""The reasoning agent and its tools.

Two reasoners are provided and share one contract:

* :class:`HeuristicReasoner` - deterministic, zero-dependency, fully
  reproducible. This is the default so evaluation numbers are meaningful.
* :class:`LLMReasoner` - optional tool-calling model backend, used when a
  compatible endpoint is configured. It may only act through the same typed
  tools, so its behaviour stays auditable.

Both return the same :class:`PolicyDecision` and both are routed through the
same confidence gates, so swapping backends does not change escalation policy.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Protocol

from .config import PolicyConfig
from .observability import ToolTrace
from .policy import Evidence, PolicyDecision, confidence_label, evaluate_evidence


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
You are given bounded numeric facts about one evidence bundle, gathered by tools.
Judge the evidence against these gates: motion_present, salient_subject,
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
        import httpx

        base, key = self._endpoint()
        tools = ToolRegistry(trace)
        summary = tools.clip_summary(evidence)
        profile = tools.motion_profile(evidence)
        timeline = tools.timeline(evidence)

        facts = {"clip": summary, "motion": profile, "timeline": timeline}
        prompt = (
            "Evidence bundle facts:\n"
            + json.dumps(facts, ensure_ascii=False)
            + "\n\nJudge against the gates. Return JSON only."
        )

        with trace.call("llm_reason", model=self.model):
            resp = httpx.post(
                f"{base}/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json={
                    "model": self.model,
                    "temperature": 0.0,
                    "max_tokens": 200,
                    "messages": [
                        {"role": "system", "content": LLM_SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                },
                timeout=30.0,
            )
            resp.raise_for_status()
            self.calls += 1
            content = resp.json()["choices"][0]["message"]["content"].strip()

        parsed = json.loads(content[content.find("{"): content.rfind("}") + 1])
        confidence = max(0.0, min(1.0, float(parsed.get("confidence", 0.0))))
        scored = PolicyDecision(
            decision="candidate",
            confidence=round(confidence, 4),
            rules=evaluate_evidence(evidence).rules,
            rationale=str(parsed.get("rationale", ""))[:400],
        )
        return apply_gates(scored, self.cfg)


def build_reasoner(cfg: PolicyConfig, backend: str = "heuristic") -> Reasoner:
    """Factory so the backend is a config value rather than a code path."""

    if backend == "llm":
        return LLMReasoner(cfg)
    return HeuristicReasoner(cfg)


__all__ = [
    "HeuristicReasoner",
    "LLMReasoner",
    "Reasoner",
    "ToolRegistry",
    "apply_gates",
    "build_reasoner",
    "confidence_label",
]