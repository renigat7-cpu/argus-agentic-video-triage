"""Failure-path tests: bad input becomes a recorded escalation, not a 400."""

from __future__ import annotations

from pathlib import Path

import pytest

from argus.config import PipelineConfig
from argus.pipeline import TriagePipeline
from argus.policy import PolicyDecision


def _pipeline(tmp_path: Path) -> TriagePipeline:
    return TriagePipeline(PipelineConfig(out_dir=tmp_path / "out"), reasoner_backend="heuristic")


def test_non_video_file_escalates_instead_of_raising(tmp_path):
    junk = tmp_path / "not-a-video.mp4"
    junk.write_bytes(b"this is definitely not an mp4 container" * 64)
    pipe = _pipeline(tmp_path)

    result = pipe.triage(junk, location="cam-3")

    assert result.decision == "escalate"
    assert result.rationale.startswith("decode_error")
    assert result.confidence == 0.0
    assert result.confidence_label == "low"
    assert result.n_frames_scanned == 0
    assert result.error

    # The failure is recorded like any other decision, and reaches the human queue.
    log = (tmp_path / "out" / "decisions.jsonl").read_text(encoding="utf-8")
    assert "decode_error" in log
    assert pipe.escalations.count("open") == 1


def test_empty_file_escalates(tmp_path):
    empty = tmp_path / "empty.mp4"
    empty.write_bytes(b"")
    result = _pipeline(tmp_path).triage(empty)

    assert result.decision == "escalate"
    assert result.rationale.startswith("decode_error")


def test_broken_reasoner_degrades_to_heuristic(tmp_path):
    class ExplodingReasoner:
        name = "exploding-v1"

        def reason(self, evidence, trace):  # noqa: ANN001, ANN201
            raise RuntimeError("model gateway down")

    from argus.eval import make_synthetic_clip

    clip = make_synthetic_clip(
        tmp_path / "clip.mp4", duration_s=3.0, events=[(1.0, 2.0)], seed=1
    ).clip
    pipe = TriagePipeline(PipelineConfig(out_dir=tmp_path / "out"), reasoner=ExplodingReasoner())

    result = pipe.triage(clip)

    assert result.decision in {"confirm", "escalate", "dismiss"}
    assert result.rationale.startswith("reasoner_fallback: ")
    assert any(c["name"] == "reasoner_error" for c in result.tool_calls)
    assert not isinstance(result.decision, PolicyDecision)


def test_api_returns_200_for_an_undecodable_upload(tmp_path, monkeypatch):
    pytest.importorskip("starlette.testclient")
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ARGUS_OUT_DIR", str(tmp_path / "out"))
    monkeypatch.setenv("ARGUS_CONFIG", "")
    monkeypatch.setenv("ARGUS_AWS_SINK", "0")

    from argus.api import app

    client = TestClient(app)
    junk = b"still not a video" * 128

    resp = client.post(
        "/api/triage",
        files={"file": ("junk.mp4", junk, "video/mp4")},
        data={"location": "cam-7"},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["decision"] == "escalate"
    assert body["rationale"].startswith("decode_error")
    assert body["clip"] == "junk.mp4"


def test_capabilities_route_reports_the_tool_surface():
    pytest.importorskip("starlette.testclient")
    from fastapi.testclient import TestClient

    from argus.api import app

    body = TestClient(app).get("/api/capabilities").json()

    assert body["human_loop"] is True
    assert body["tools"] == ["clip_summary", "motion_profile", "timeline", "gate_check"]
    assert body["reasoner"]
    assert "opencv" in body
    assert "bedrock" in body and "aws_sink" in body
