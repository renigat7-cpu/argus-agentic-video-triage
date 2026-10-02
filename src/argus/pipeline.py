"""End-to-end pipeline: ingest -> select -> reason -> gate -> record.

One :class:`TriageResult` per clip, always accompanied by the diagnostics the
evaluation harness needs (frames scanned, frames selected, cost, latency) so
quality and cost can be traded off explicitly rather than implicitly.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .agent import Reasoner, build_reasoner
from .config import PipelineConfig
from .ingest import ClipMeta, extract_signals
from .observability import DecisionRecord, Recorder, ToolTrace
from .policy import Evidence, confidence_label
from .selection import Candidate, select_frames


@dataclass
class TriageResult:
    """Outcome of triaging one clip."""

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
    reduction_ratio: float = 0.0
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    selection: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
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
            "reduction_ratio": self.reduction_ratio,
            "cost_usd": round(self.cost_usd, 8),
            "latency_ms": round(self.latency_ms, 2),
            "selection": self.selection,
        }


class TriagePipeline:
    """Stateless-per-clip pipeline with an optional shared recorder."""

    def __init__(
        self,
        cfg: PipelineConfig | None = None,
        reasoner: Reasoner | None = None,
        reasoner_backend: str = "heuristic",
        recorder: Recorder | None = None,
    ) -> None:
        self.cfg = cfg or PipelineConfig()
        self.reasoner = reasoner or build_reasoner(self.cfg.policy, reasoner_backend)
        self.recorder = recorder or Recorder(self.cfg)

    def triage(
        self, clip: str | Path, location: str = "unknown", max_frames: int | None = None
    ) -> TriageResult:
        """Triage a single clip and persist the decision record."""

        started = time.perf_counter()
        run_id = uuid.uuid4().hex[:12]

        signals, meta = extract_signals(clip, self.cfg.selection, max_frames=max_frames)
        candidates, diag = select_frames(signals, self.cfg.selection)
        chosen: list[Candidate] = [c for c in candidates if c.selected]

        evidence = Evidence(
            frames=[c.signals for c in chosen],
            clip_duration_s=meta.duration_s,
            fps=meta.fps,
            width=meta.width,
            height=meta.height,
            location=location,
        )

        trace = ToolTrace()
        decision = self.reasoner.reason(evidence, trace)

        latency_ms = (time.perf_counter() - started) * 1000.0
        cost = (
            self.cfg.cost.frame_cost(len(signals))
            + self.cfg.cost.reasoning_call_cost(len(trace.calls))
        )

        result = TriageResult(
            run_id=run_id,
            clip=str(clip),
            decision=decision.decision,
            confidence=decision.confidence,
            confidence_label=confidence_label(decision.confidence),
            rationale=decision.rationale,
            rules=[
                {
                    "rule": r.rule,
                    "passed": r.passed,
                    "confidence": r.confidence,
                    "rationale": r.rationale,
                }
                for r in decision.rules
            ],
            tool_calls=trace.to_list(),
            n_frames_scanned=len(signals),
            n_frames_selected=len(chosen),
            reduction_ratio=float(diag.get("reduction_ratio", 0.0)),
            cost_usd=cost,
            latency_ms=latency_ms,
            selection=[
                {"t": c.signals.timestamp_s, "score": c.score} for c in chosen
            ],
        )

        self.recorder.record_decision(
            DecisionRecord(
                run_id=run_id,
                clip=str(clip),
                decision=result.decision,
                confidence=result.confidence,
                confidence_label=result.confidence_label,
                rationale=result.rationale,
                rules=result.rules,
                tool_calls=result.tool_calls,
                n_frames_scanned=result.n_frames_scanned,
                n_frames_selected=result.n_frames_selected,
                cost_usd=result.cost_usd,
                latency_ms=result.latency_ms,
            )
        )
        return result

    def triage_many(
        self, clips: list[str | Path], location: str = "unknown"
    ) -> list[TriageResult]:
        return [self.triage(c, location=location) for c in clips]


def summarize(results: list[TriageResult]) -> dict[str, Any]:
    """Aggregate a batch for reports and the API."""

    if not results:
        return {"n": 0}
    by_decision: dict[str, int] = {}
    for r in results:
        by_decision[r.decision] = by_decision.get(r.decision, 0) + 1
    scanned = sum(r.n_frames_scanned for r in results)
    selected = sum(r.n_frames_selected for r in results)
    return {
        "n": len(results),
        "by_decision": by_decision,
        "frames_scanned": scanned,
        "frames_selected": selected,
        "reduction_ratio": round(1.0 - selected / scanned, 4) if scanned else 0.0,
        "total_cost_usd": round(sum(r.cost_usd for r in results), 8),
        "mean_latency_ms": round(sum(r.latency_ms for r in results) / len(results), 2),
        "escalation_rate": round(
            sum(1 for r in results if r.decision == "escalate") / len(results), 4
        ),
    }


def clip_meta_dict(meta: ClipMeta) -> dict[str, Any]:
    return {
        "path": meta.path,
        "width": meta.width,
        "height": meta.height,
        "fps": meta.fps,
        "n_frames": meta.n_frames,
        "duration_s": round(meta.duration_s, 3),
    }