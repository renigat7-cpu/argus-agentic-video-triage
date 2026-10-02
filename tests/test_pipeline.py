"""End-to-end smoke test on a tiny synthetic clip (needs OpenCV)."""

from __future__ import annotations

from pathlib import Path

from argus.config import PipelineConfig
from argus.eval import make_synthetic_clip
from argus.pipeline import TriagePipeline


def _clip(tmp_path: Path, has_event: bool) -> str:
    events = [(1.0, 2.0)] if has_event else []
    return make_synthetic_clip(
        tmp_path / ("pos.mp4" if has_event else "neg.mp4"),
        duration_s=3.0,
        fps=25.0,
        events=events,
        seed=1,
    ).clip


def test_pipeline_confirms_motion_and_records(tmp_path):
    cfg = PipelineConfig(out_dir=tmp_path / "out")
    pipe = TriagePipeline(cfg, reasoner_backend="heuristic")

    result = pipe.triage(_clip(tmp_path, has_event=True), location="test")

    assert result.decision in {"confirm", "escalate"}
    assert result.n_frames_scanned > 0
    assert result.n_frames_selected <= result.n_frames_scanned
    assert result.tool_calls  # the agent actually used its tools
    assert (tmp_path / "out" / cfg.decision_log).exists()


def test_pipeline_does_not_confirm_without_motion(tmp_path):
    cfg = PipelineConfig(out_dir=tmp_path / "out")
    pipe = TriagePipeline(cfg, reasoner_backend="heuristic")

    result = pipe.triage(_clip(tmp_path, has_event=False), location="test")

    assert result.decision != "confirm"
