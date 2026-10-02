"""Observability tests: decisions must be reconstructable from the log."""

from __future__ import annotations

import json

from argus.config import PipelineConfig
from argus.observability import DecisionRecord, Recorder, ToolTrace


def test_recorder_appends_jsonl(tmp_path):
    cfg = PipelineConfig(out_dir=tmp_path)
    rec = Recorder(cfg)
    record = DecisionRecord(
        run_id="r1",
        clip="a.mp4",
        decision="escalate",
        confidence=0.55,
        confidence_label="medium",
        rationale="ambiguous",
        rules=[{"rule": "motion_present", "passed": True, "confidence": 0.8}],
    )
    rec.record_decision(record)
    rec.record_decision(record)

    rows = rec.decisions()
    assert len(rows) == 2
    assert rows[0]["decision"] == "escalate"
    assert rows[0]["rules"][0]["rule"] == "motion_present"


def test_tool_trace_records_failures_without_raising():
    trace = ToolTrace()
    try:
        with trace.call("boom", x=1) as status:
            status["summary"] = "did something"
            raise ValueError("nope")
    except ValueError:
        pass
    # A failing tool is recorded as data, not a crash.
    assert trace.to_list()[0]["ok"] is False
    assert trace.to_list()[0]["name"] == "boom"


def test_run_log_written(tmp_path):
    cfg = PipelineConfig(out_dir=tmp_path)
    rec = Recorder(cfg)
    rec.record_run({"clips": 3, "reasoner": "heuristic"})
    data = json.loads(rec.runs_path.read_text(encoding="utf-8").strip())
    assert data["clips"] == 3


def test_config_roundtrip(tmp_path):
    from argus.config import load_config

    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({"selection": {"top_k": 3}}), encoding="utf-8")
    cfg = load_config(p)
    assert cfg.selection.top_k == 3