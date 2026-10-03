"""Frame selection: turn a clip into a small, well-spread evidence set.

Naive top-K by motion is the obvious approach and it fails in a specific,
reproducible way: one busy burst produces K adjacent frames and the rest of the
clip is never inspected. ``select_frames`` therefore scores cheaply, then applies
two diversity constraints before taking the top-K:

* **temporal** - never two frames closer than ``diversity_ms``;
* **visual** (OpenCV 5) - never two frames whose ``cv2.img_hash`` perceptual
  hashes are within ``PHASH_MAX_DISTANCE`` bits of each other while also being
  close in time. A static scene produces high-confidence motion in every frame
  of a burst; the perceptual hash is what tells those frames apart from genuinely
  different moments of the same clip.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from .config import SelectionConfig
from .signals import FrameSignals

PHASH_MAX_DISTANCE = 6
"""Hamming distance under which two 64-bit perceptual hashes are near-duplicates."""

PHASH_WINDOW_MS = 3000
"""Near-duplicate hashes only count as redundant when this close in time."""


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


def hamming(a: str, b: str) -> int:
    """Bit distance between two hex perceptual hashes, or -1 if incomparable."""

    if not a or not b or len(a) != len(b):
        return -1
    try:
        return bin(int(a, 16) ^ int(b, 16)).count("1")
    except ValueError:
        return -1


def is_near_duplicate(a: FrameSignals, b: FrameSignals) -> bool:
    """True when two frames look the same *and* were sampled close together."""

    distance = hamming(a.phash, b.phash)
    if distance < 0 or distance > PHASH_MAX_DISTANCE:
        return False
    return abs(a.timestamp_s - b.timestamp_s) * 1000.0 < PHASH_WINDOW_MS


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
    # Visual diversity (OpenCV 5 img_hash): never take a near-duplicate frame.
    chosen: list[Candidate] = []
    duplicates: set[tuple[int, float]] = set()
    for cand in ranked:
        if len(chosen) >= cfg.top_k:
            break
        if any(
            abs(cand.signals.timestamp_s - c.signals.timestamp_s) * 1000.0
            < cfg.diversity_ms
            for c in chosen
        ):
            continue
        if any(is_near_duplicate(cand.signals, c.signals) for c in chosen):
            duplicates.add((cand.signals.index, cand.signals.timestamp_s))
            continue
        chosen.append(cand)

    chosen_set = {(c.signals.index, c.signals.timestamp_s) for c in chosen}
    final: list[Candidate] = []
    for c in scored:
        key = (c.signals.index, c.signals.timestamp_s)
        if key in chosen_set:
            final.append(Candidate(c.signals, c.score, selected=True, rejected_reason=""))
        elif c.rejected_reason:
            final.append(
                Candidate(c.signals, c.score, selected=False, rejected_reason=c.rejected_reason)
            )
        elif key in duplicates:
            final.append(
                Candidate(
                    c.signals, c.score, selected=False, rejected_reason="phash-near-duplicate"
                )
            )
        else:
            final.append(
                Candidate(
                    c.signals, c.score, selected=False, rejected_reason="diversity-constraint"
                )
            )

    diag = {
        "scored": float(len(scored)),
        "floor": round(floor, 6),
        "selected": float(len(chosen)),
        "phash_duplicates": float(len(duplicates)),
        "reduction_ratio": round(1.0 - (len(chosen) / max(1, len(scored))), 6),
    }
    return final, diag


def selected(signals: Iterable[FrameSignals], candidates: Iterable[Candidate]) -> list[FrameSignals]:
    """Reorder helper: pull the selected frames back out in temporal order."""

    by_key = {(c.signals.index, c.signals.timestamp_s): c for c in candidates}
    chosen = [s for s in signals if by_key.get((s.index, s.timestamp_s), None)
              and by_key[(s.index, s.timestamp_s)].selected]
    return sorted(chosen, key=lambda s: s.timestamp_s)
