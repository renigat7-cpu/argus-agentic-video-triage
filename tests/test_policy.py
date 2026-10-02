"""Policy and escalation tests: the escalation band must be respected."""

from __future__ import annotations

from argus.agent import apply_gates
from argus.config import PolicyConfig
from argus.policy import Evidence, RuleResult, evaluate_evidence
from argus.signals import FrameSignals


def _evidence(motion: float, n: int = 5, luma: float = 0.5) -> Evidence:
    frames = [
        FrameSignals(
            index=i,
            timestamp_s=i * 0.25,
            motion=motion,
            optical_flow_mag=motion,
            edge_density=0.1,
            saliency=0.3 if motion > 0.2 else 0.01,
            color_anomaly=0.02,
            luma=luma,
        )
        for i in range(n)
    ]
    return Evidence(frames=frames, clip_duration_s=10.0, fps=25.0, width=640, height=360)


def test_high_motion_confirms():
    decision = evaluate_evidence(_evidence(motion=1.0))
    assert decision.confidence >= 0.8


def test_static_scene_is_low_confidence():
    decision = evaluate_evidence(_evidence(motion=0.0))
    assert decision.confidence < 0.5


def test_escalation_band():
    cfg = PolicyConfig(confirm_at=0.8, dismiss_at=0.35)
    scored = evaluate_evidence(_evidence(motion=0.35))
    gated = apply_gates(scored, cfg)
    assert gated.decision in ("confirm", "dismiss", "escalate")
    if cfg.dismiss_at < gated.confidence < cfg.confirm_at:
        assert gated.decision == "escalate"


def test_dark_clip_fails_illumination_gate():
    decision = evaluate_evidence(_evidence(motion=1.0, luma=0.001))
    failed = [r.rule for r in decision.rules if not r.passed]
    assert "illumination_usable" in failed
    # A failed gate must cap confidence below the confirm threshold.
    assert decision.confidence <= 0.79


def test_no_motion_cannot_auto_confirm_even_at_high_confidence():
    cfg = PolicyConfig(require_motion_for_confirm=True)
    decision = apply_gates(
        _scored(confidence=0.95, rules=[RuleResult("illumination_usable", True, 1.0, "")]),
        cfg,
    )
    assert decision.decision == "escalate"


def _scored(confidence: float, rules):
    from argus.policy import PolicyDecision

    return PolicyDecision(decision="candidate", confidence=confidence, rules=rules)