"""Observability: every decision and every tool call is recorded.

This module is deliberately dependency-free and append-only. A judge should be
able to open one JSONL file and reconstruct exactly why the system escalated a
clip, what it cost, and which thresholds were live at the time.
"""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from .config import PipelineConfig


def _utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@dataclass
class ToolCall:
    """A single typed tool invocation made by the agent."""

    name: str
    args: dict[str, Any]
    duration_ms: float
    ok: bool
    result_summary: str = ""


@dataclass
class DecisionRecord:
    """One end-to-end decision about one evidence bundle."""

    run_id: str
    clip: str
    decision: str
    confidence: float
    confidence_label: str
    rationale: str
    rules: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    n_frames_scanned: int = 0
    n_frames_selected: int = 0
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    timestamp: str = field(default_factory=_utc)

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "run_id": self.run_id,
            "clip": self.clip,
            "decision": self.decision,
            "confidence": self.confidence,
            "confidence_label": self.confidence_label,
            "rationale": self.rationale,
            "rules": self.rules,
            "tool_calls": self.tool_calls,
            "n_frames_scanned": self.n_frames_scanned,
            "n_frames_selected": self.n_frames_selected,
            "cost_usd": round(self.cost_usd, 8),
            "latency_ms": round(self.latency_ms, 2),
        }


class Recorder:
    """Append-only JSONL recorder for decisions and runs."""

    def __init__(self, cfg: PipelineConfig) -> None:
        cfg.ensure_dirs()
        self.cfg = cfg
        self.decisions_path = cfg.out_dir / cfg.decision_log
        self.runs_path = cfg.out_dir / cfg.run_log

    def record_decision(self, rec: DecisionRecord) -> None:
        with self.decisions_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec.to_dict(), ensure_ascii=False) + "\n")

    def record_run(self, payload: dict[str, Any]) -> None:
        with self.runs_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"timestamp": _utc(), **payload}, ensure_ascii=False) + "\n")

    def decisions(self) -> list[dict[str, Any]]:
        if not self.decisions_path.is_file():
            return []
        out: list[dict[str, Any]] = []
        for line in self.decisions_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                out.append(json.loads(line))
        return out


class ToolTrace:
    """Collects tool calls produced while reasoning about one bundle."""

    def __init__(self) -> None:
        self.calls: list[ToolCall] = []

    @contextmanager
    def call(self, name: str, **args: Any) -> Iterator[dict[str, Any]]:
        started = time.perf_counter()
        status: dict[str, Any] = {"ok": True, "summary": ""}
        try:
            yield status
        except Exception as exc:  # noqa: BLE001 — tool failure is data, not a crash
            status["ok"] = False
            status["summary"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            elapsed = (time.perf_counter() - started) * 1000.0
            self.calls.append(
                ToolCall(
                    name=name,
                    args=args,
                    duration_ms=round(elapsed, 3),
                    ok=status["ok"],
                    result_summary=str(status.get("summary", ""))[:200],
                )
            )

    def to_list(self) -> list[dict[str, Any]]:
        return [
            {
                "name": c.name,
                "args": c.args,
                "duration_ms": c.duration_ms,
                "ok": c.ok,
                "result": c.result_summary,
            }
            for c in self.calls
        ]