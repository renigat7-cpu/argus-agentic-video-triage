"""Frame selection: turn a clip into a small, well-spread evidence set.

Naive top-K by motion is the obvious approach and it fails in a specific,
reproducible way: one busy burst produces K adjacent frames and the rest of the
clip is never inspected. ``select_frames`` therefore scores cheaply, then applies
a temporal diversity constraint before taking the top-K.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from .config import SelectionConfig
from .signals import FrameSignals


@dataclass(frozen=True)
class Candidate:
    """A frame proposed for expensive analysis."""

    signals: FrameSignals
    score: float
    selected: bool = False
    rejected_reason: str = ""


def combine_score(s: FrameSignals, cfg: SelectionConfig) -> float:
    """Weighted blend of the cheap signals into one ranking score."""

    motion = max(s.motion, s.optical_flow_mag)
    return (
        cfg.motion_weight * motion
        + cfg.edge_weight * s.edge_density
        + cfg.saliency_weight * s.saliency
        + cfg.color_weight * s.color_anomaly
    )


def _motion_floor(signals: Sequence[FrameSignals], percentile: float) -> float:
    """Threshold below which frames are considered background noise."""

    motions = [s.motion for s in signals]
    if not motions:
        return 0.0
    return float(__import__("numpy").percentile(motions, percentile))


def select_frames(
    signals: Sequence[FrameSignals], cfg: SelectionConfig
) -> tuple[list[Candidate], dict[str, float]]:
    """Select up to ``cfg.top_k`` frames, spread across the clip.

    Returns the scored candidates (selected flag included) and diagnostics that
    the eval harness records, so a selection change can be attributed to a
    score change rather than a timing change.
    """

    if not signals:
        return [], {"scored": 0.0, "floor": 0.0, "selected": 0.0}

    floor = _motion_floor(signals, cfg.motion_percentile)
    scored: list[Candidate] = []

    for s in signals:
        score = combine_score(s, cfg)
        reason = ""
        if s.motion < floor and s.color_anomaly < 0.05:
            reason = "below-motion-floor"
        elif score < cfg.min_score:
            reason = "below-min-score"
        scored.append(Candidate(signals=s, score=round(score, 6), rejected_reason=reason))

    ranked = sorted(
        (c for c in scored if not c.rejected_reason),
        key=lambda c: (-c.score, c.signals.timestamp_s),
    )

    # Temporal diversity: never take two frames closer than diversity_ms.
    chosen: list[Candidate] = []
    for cand in ranked:
        if len(chosen) >= cfg.top_k:
            break
        if any(
            abs(cand.signals.timestamp_s - c.signals.timestamp_s) * 1000.0
            < cfg.diversity_ms
            for c in chosen
        ):
            continue
        chosen.append(cand)

    chosen_set = {(c.signals.index, c.signals.timestamp_s) for c in chosen}
    final: list[Candidate] = []
    for c in scored:
        key = (c.signals.index, c.signals.timestamp_s)
        if key in chosen_set:
            final.append(Candidate(c.signals, c.score, selected=True, rejected_reason=""))
        else:
            reason = c.rejected_reason or (
                "diversity-constraint" if not c.rejected_reason else c.rejected_reason
            )
            final.append(Candidate(c.signals, c.score, selected=False, rejected_reason=reason))

    diag = {
        "scored": float(len(scored)),
        "floor": round(floor, 6),
        "selected": float(len(chosen)),
        "reduction_ratio": round(1.0 - (len(chosen) / max(1, len(scored))), 6),
    }
    return final, diag


def selected(signals: Iterable[FrameSignals], candidates: Iterable[Candidate]) -> list[FrameSignals]:
    """Reorder helper: pull the selected frames back out in temporal order."""

    by_key = {(c.signals.index, c.signals.timestamp_s): c for c in candidates}
    chosen = [s for s in signals if by_key.get((s.index, s.timestamp_s), None)
              and by_key[(s.index, s.timestamp_s)].selected]
    return sorted(chosen, key=lambda s: s.timestamp_s)