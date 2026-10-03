"""Selection tests: the diversity constraint is the whole point."""

from __future__ import annotations

from argus.config import SelectionConfig
from argus.selection import combine_score, select_frames
from argus.signals import FrameSignals


def _sig(index: int, t: float, motion: float, phash: str = "") -> FrameSignals:
    return FrameSignals(
        index=index,
        timestamp_s=t,
        motion=motion,
        optical_flow_mag=motion,
        edge_density=0.1,
        saliency=0.2,
        color_anomaly=0.05,
        luma=0.5,
        phash=phash,
    )


def test_diversity_prevents_adjacent_burst():
    """A burst of high-motion frames must not consume the whole budget."""

    cfg = SelectionConfig(top_k=4, diversity_ms=1000, sample_fps=10)
    # 10 frames at 100ms spacing, only the first three are high motion.
    signals = [_sig(i, i * 0.1, 1.0 if i < 3 else 0.2) for i in range(10)]
    candidates, diag = select_frames(signals, cfg)
    chosen = [c for c in candidates if c.selected]

    assert len(chosen) <= cfg.top_k
    times = sorted(c.signals.timestamp_s for c in chosen)
    for a, b in zip(times, times[1:]):
        assert (b - a) * 1000 >= cfg.diversity_ms
    assert diag["selected"] == len(chosen)


def test_low_score_frames_are_dropped():
    cfg = SelectionConfig(top_k=4, min_score=0.5)
    signals = [_sig(i, i * 0.5, 0.01) for i in range(6)]
    candidates, _ = select_frames(signals, cfg)
    assert not any(c.selected for c in candidates)


def test_combine_score_is_bounded_and_positive():
    cfg = SelectionConfig()
    s = _sig(0, 0.0, 0.5)
    score = combine_score(s, cfg)
    assert 0.0 <= score < 10.0


def test_empty_signals_is_safe():
    candidates, diag = select_frames([], SelectionConfig())
    assert candidates == []
    assert diag["selected"] == 0


def test_near_duplicate_perceptual_hashes_are_rejected():
    """OpenCV 5 img_hash diversity: identical-looking frames are not evidence."""

    cfg = SelectionConfig(top_k=4, diversity_ms=10, sample_fps=10, motion_percentile=0.0)
    same = "a1a2a3a4a5a6a7a8"
    signals = [
        _sig(i, i * 0.5, 0.9, phash=same) for i in range(4)
    ] + [_sig(4, 4.0, 0.9, phash="ffffffffffffffff")]

    candidates, diag = select_frames(signals, cfg)
    chosen = [c for c in candidates if c.selected]

    assert len(chosen) == 2, "only the visually distinct frame survives the burst"
    assert {c.signals.phash for c in chosen} == {same, "ffffffffffffffff"}
    assert diag["phash_duplicates"] == 3.0
    reasons = {c.rejected_reason for c in candidates if not c.selected}
    assert "phash-near-duplicate" in reasons


def test_distant_perceptual_hashes_are_kept():
    cfg = SelectionConfig(top_k=4, diversity_ms=10, sample_fps=10, motion_percentile=0.0)
    signals = [
        _sig(0, 0.0, 0.9, phash="0000000000000000"),
        _sig(1, 1.0, 0.9, phash="ffffffffffffffff"),
    ]

    candidates, _ = select_frames(signals, cfg)

    assert len([c for c in candidates if c.selected]) == 2