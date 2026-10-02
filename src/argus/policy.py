"""Policies: explicit, inspectable rules a candidate event is judged against.

Keeping policy as data rather than prompt text is what makes decisions
auditable. Every decision Argus emits can be traced back to the exact rule and
threshold that produced it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .signals import FrameSignals


@dataclass(frozen=True)
class Evidence:
    """What the reasoner is allowed to look at."""

    frames: list[FrameSignals]
    clip_duration_s: float
    fps: float
    width: int
    height: int
    location: str = "unknown"

    @property
    def peak_motion(self) -> float:
        return max((f.motion for f in self.frames), default=0.0)

    @property
    def peak_flow(self) -> float:
        return max((f.optical_flow_mag for f in self.frames), default=0.0)

    @property
    def peak_saliency(self) -> float:
        return max((f.saliency for f in self.frames), default=0.0)

    @property
    def mean_luma(self) -> float:
        return (sum(f.luma for f in self.frames) / len(self.frames)) if self.frames else 0.0


@dataclass(frozen=True)
class RuleResult:
    rule: str
    passed: bool
    confidence: float
    rationale: str


@dataclass(frozen=True)
class PolicyDecision:
    """Outcome of judging an evidence bundle against a policy."""

    decision: str
    confidence: float
    rules: list[RuleResult] = field(default_factory=list)
    rationale: str = ""

    @property
    def needs_human(self) -> bool:
        return self.decision == "escalate"


def _rule_motion(e: Evidence, threshold: float) -> RuleResult:
    peak = max(e.peak_motion, e.peak_flow)
    ok = peak >= threshold
    return RuleResult(
        rule="motion_present",
        passed=ok,
        confidence=round(float(min(1.0, peak / max(threshold, 1e-6))), 4),
        rationale=f"peak motion/flow {peak:.3f} vs threshold {threshold:.3f}",
    )


def _rule_saliency(e: Evidence, threshold: float) -> RuleResult:
    peak = e.peak_saliency
    ok = peak >= threshold
    return RuleResult(
        rule="salient_subject",
        passed=ok,
        confidence=round(float(min(1.0, peak / max(threshold, 1e-6))), 4),
        rationale=f"peak saliency {peak:.3f} vs threshold {threshold:.3f}",
    )


def _rule_illumination(e: Evidence) -> RuleResult:
    """Reject clips too dark or washed out to judge responsibly."""

    luma = e.mean_luma
    ok = 0.06 <= luma <= 0.94
    margin = (min(luma / 0.06, 0.94 / luma) if luma > 0 else 0.0)
    return RuleResult(
        rule="illumination_usable",
        passed=ok,
        confidence=round(float(min(1.0, margin)), 4),
        rationale=f"mean luma {luma:.3f}",
    )


def _rule_persistence(e: Evidence, min_frames: int) -> RuleResult:
    """Real events persist; single-frame artefacts do not."""

    n = len(e.frames)
    ok = n >= min_frames
    return RuleResult(
        rule="temporal_persistence",
        passed=ok,
        confidence=round(float(min(1.0, n / max(1, min_frames))), 4),
        rationale=f"{n} evidence frames, need >= {min_frames}",
    )


RULES: dict[str, Callable[[Evidence], RuleResult]] = {
    "motion_present": lambda e: _rule_motion(e, 0.18),
    "salient_subject": lambda e: _rule_saliency(e, 0.22),
    "illumination_usable": _rule_illumination,
    "temporal_persistence": lambda e: _rule_persistence(e, 3),
}

# Rule weights for the evidence-weighted confidence.
RULE_WEIGHTS: dict[str, float] = {
    "motion_present": 0.35,
    "salient_subject": 0.25,
    "illumination_usable": 0.20,
    "temporal_persistence": 0.20,
}


def evaluate_evidence(evidence: Evidence) -> PolicyDecision:
    """Run every rule and fold the results into a decision.

    The fold is deliberately a weighted average of per-rule confidences rather
    than a hard AND: a partially satisfied policy should surface as medium
    confidence, which routes to a human instead of silently confirming.
    """

    results = [rule(evidence) for rule in RULES.values()]

    weighted = sum(
        RULE_WEIGHTS[r.rule] * r.confidence for r in results
    )
    failed = [r for r in results if not r.passed]
    confidence = round(float(weighted), 4)

    # Any failed gate caps confidence: we never auto-confirm past a hard gate.
    if failed:
        confidence = min(confidence, 0.79)
        rationale = "failed gates: " + ", ".join(r.rule for r in failed)
    else:
        rationale = "all gates satisfied"

    return PolicyDecision(
        decision="candidate",
        confidence=confidence,
        rules=results,
        rationale=rationale,
    )


def confidence_label(confidence: float) -> str:
    if confidence >= 0.8:
        return "high"
    if confidence >= 0.35:
        return "medium"
    return "low"