"""Human-in-the-loop tests: an escalation is only real if it can be answered."""

from __future__ import annotations

import pytest

from argus.config import PipelineConfig
from argus.human import EscalationQueue


def _queue(tmp_path) -> EscalationQueue:
    return EscalationQueue(PipelineConfig(out_dir=tmp_path / "out"))


def _decision(run_id: str = "r1", confidence: float = 0.5) -> dict:
    return {
        "run_id": run_id,
        "clip": "clip.mp4",
        "confidence": confidence,
        "confidence_label": "medium",
        "rationale": "confidence 0.5 in escalation band",
        "decision": "escalate",
    }


def test_enqueue_then_list_open(tmp_path):
    queue = _queue(tmp_path)

    row = queue.enqueue(_decision())

    assert row["status"] == "open"
    assert row["run_id"] == "r1"
    assert queue.list() == [row]
    assert queue.count("open") == 1
    assert queue.escalations_path.is_file()


def test_verdict_supersedes_the_open_escalation(tmp_path):
    queue = _queue(tmp_path)
    queue.enqueue(_decision())

    verdict = queue.verdict("r1", "reject", reviewer="romashik", note="static scene")

    assert verdict["verdict"] == "reject"
    assert verdict["status"] == "reject"
    assert queue.list("open") == [], "a resolved escalation leaves the open queue"
    resolved = queue.list("reject")
    assert len(resolved) == 1
    assert resolved[0]["reviewer"] == "romashik"
    assert resolved[0]["note"] == "static scene"
    assert resolved[0]["clip"] == "clip.mp4", "escalation context survives the merge"
    assert queue.verdicts_path.is_file()


def test_several_escalations_are_tracked_independently(tmp_path):
    queue = _queue(tmp_path)
    queue.enqueue(_decision("r1"))
    queue.enqueue(_decision("r2"))
    queue.verdict("r1", "approve")

    assert {r["run_id"] for r in queue.list("open")} == {"r2"}
    assert queue.count() == 1
    assert queue.count(None) == 2
    assert queue.list("approve")[0]["run_id"] == "r1"


def test_unknown_verdict_is_rejected(tmp_path):
    queue = _queue(tmp_path)
    with pytest.raises(ValueError):
        queue.verdict("r1", "maybe")


def test_latest_verdict_wins(tmp_path):
    queue = _queue(tmp_path)
    queue.enqueue(_decision("r1"))
    queue.verdict("r1", "needs_more")
    queue.verdict("r1", "approve")

    assert queue.count("open") == 0
    assert queue.list("approve")[0]["verdict"] == "approve"
