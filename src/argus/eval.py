"""Evaluation harness.

Evaluation is where a system like this either earns credibility or does not, so
the harness does two things on purpose:

1. It runs against **labelled data it generates itself**, so the numbers are
   reproducible by anyone and do not depend on a private dataset.
2. It reports the metric that actually matters operationally - *missed events*
   - next to precision, because a triage system that quietly drops a real event
   is worse than one that escalates too often.

Usage::

    python -m argus.eval --clips 12 --out var/eval
"""

from __future__ import annotations

import argparse
import json
import math
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from .config import PipelineConfig
from .pipeline import TriagePipeline, TriageResult, summarize


@dataclass(frozen=True)
class GroundTruth:
    """What actually happens in a synthetic clip."""

    clip: str
    has_event: bool
    event_intervals: list[tuple[float, float]]
    duration_s: float

    def overlaps_any(self, times: list[float]) -> bool:
        return any(
            any(start <= t <= end for start, end in self.event_intervals)
            for t in times
        )


def make_synthetic_clip(
    path: Path,
    duration_s: float = 20.0,
    fps: float = 25.0,
    events: list[tuple[float, float]] | None = None,
    width: int = 640,
    height: int = 360,
    seed: int = 0,
) -> GroundTruth:
    """Render a clip with a static scene plus optional moving objects.

    The scene is deliberately low-contrast and the objects small, so a naive
    "detect big motion" baseline will not trivially solve the task.
    """

    rng = np.random.default_rng(seed)
    events = events or []
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(path), fourcc, fps, (width, height))
    if not writer.isOpened():
        raise RuntimeError(f"cannot open writer for {path}")

    # Static textured background so edge density is not trivially constant.
    background = np.full((height, width, 3), 60, dtype=np.uint8)
    background = cv2.add(background, rng.integers(0, 25, (height, width, 3), dtype=np.uint8))
    for _ in range(40):
        x1, y1 = rng.integers(0, width - 40, 2)
        x2, y2 = x1 + rng.integers(10, 40), y1 + rng.integers(10, 40)
        cv2.rectangle(background, (int(x1), int(y1)), (int(x2), int(y2)),
                      (int(rng.integers(40, 90)),) * 3, -1)

    n = int(duration_s * fps)
    for i in range(n):
        t = i / fps
        frame = background.copy()
        for (start, end) in events:
            if start <= t <= end:
                # Small fast-moving object: easy to miss at low sample rates.
                x = int(40 + (t - start) * 55 % (width - 120))
                y = int(height * 0.62 + 14 * math.sin(t * 3.0))
                cv2.rectangle(frame, (x, y), (x + 26, y + 34), (30, 220, 240), -1)
                cv2.circle(frame, (x + 13, y + 17), 9, (20, 20, 220), -1)
        frame = cv2.GaussianBlur(frame, (3, 3), 0)
        writer.write(frame)

    writer.release()
    return GroundTruth(
        clip=str(path),
        has_event=bool(events),
        event_intervals=list(events),
        duration_s=duration_s,
    )


@dataclass
class Metrics:
    """Clip-level metrics plus the operational numbers."""

    n_clips: int
    positives: int
    negatives: int
    true_positives: int
    false_positives: int
    false_negatives: int
    true_negatives: int
    escalations: int
    confirmed: int
    dismissed: int
    missed_event_rate: float
    precision: float
    recall: float
    f1: float
    escalation_rate: float
    selection_recall: float
    frames_scanned: int
    frames_selected: int
    reduction_ratio: float
    cost_usd: float
    cost_per_stream_hour_usd: float
    mean_latency_ms: float
    baseline_precision: float
    baseline_recall: float
    baseline_f1: float
    baseline_missed_event_rate: float
    baseline_false_positives: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _safe_div(a: float, b: float) -> float:
    return a / b if b else 0.0


def evaluate(
    pipeline: TriagePipeline, truths: list[GroundTruth]
) -> tuple[Metrics, list[dict[str, Any]], list[TriageResult]]:
    """Run the pipeline over labelled clips and compute metrics.

    ``selection_recall`` is the fraction of positive clips where selection
    actually surfaced an event frame. It is the diagnostic that separates
    "the agent decided wrong" from "the agent never saw the event".
    """

    results: list[TriageResult] = []
    rows: list[dict[str, Any]] = []
    selection_hits = 0

    for truth in truths:
        res = pipeline.triage(truth.clip, location="synthetic-lab")
        results.append(res)

        selected_times = [float(s["t"]) for s in res.selection]
        saw_event = truth.overlaps_any(selected_times)
        if truth.has_event and saw_event:
            selection_hits += 1

        confirmed = res.decision == "confirm"
        rows.append(
            {
                "clip": truth.clip,
                "has_event": truth.has_event,
                "decision": res.decision,
                "confidence": res.confidence,
                "selected_times": selected_times,
                "selection_saw_event": saw_event,
                "correct": (confirmed == truth.has_event),
            }
        )

    tp = sum(1 for t, r in zip(truths, results)
             if t.has_event and r.decision == "confirm")
    fn = sum(1 for t, r in zip(truths, results)
             if t.has_event and r.decision != "confirm")
    fp = sum(1 for t, r in zip(truths, results)
             if not t.has_event and r.decision == "confirm")
    tn = sum(1 for t, r in zip(truths, results)
             if not t.has_event and r.decision != "confirm")

    positives = sum(1 for t in truths if t.has_event)
    negatives = len(truths) - positives

    # Ablation: a naive "motion-only trigger" (no agent aggregation).
    def _motion_only(r: TriageResult) -> bool:
        return any(rule["rule"] == "motion_present" and rule["passed"] for rule in r.rules)

    b_tp = sum(1 for t, r in zip(truths, results) if t.has_event and _motion_only(r))
    b_fn = sum(1 for t, r in zip(truths, results) if t.has_event and not _motion_only(r))
    b_fp = sum(1 for t, r in zip(truths, results) if not t.has_event and _motion_only(r))
    b_precision = _safe_div(b_tp, b_tp + b_fp)
    b_recall = _safe_div(b_tp, b_tp + b_fn)

    escalations = sum(1 for r in results if r.decision == "escalate")
    scanned = sum(r.n_frames_scanned for r in results)
    selected = sum(r.n_frames_selected for r in results)
    total_seconds = sum(t.duration_s for t in truths)
    total_cost = sum(r.cost_usd for r in results)

    precision = _safe_div(tp, tp + fp)
    recall = _safe_div(tp, tp + fn)

    metrics = Metrics(
        n_clips=len(truths),
        positives=positives,
        negatives=negatives,
        true_positives=tp,
        false_positives=fp,
        false_negatives=fn,
        true_negatives=tn,
        escalations=escalations,
        confirmed=sum(1 for r in results if r.decision == "confirm"),
        dismissed=sum(1 for r in results if r.decision == "dismiss"),
        missed_event_rate=round(_safe_div(fn, positives), 4),
        precision=round(precision, 4),
        recall=round(recall, 4),
        f1=round(_safe_div(2 * precision * recall, precision + recall), 4),
        escalation_rate=round(_safe_div(escalations, len(results)), 4),
        selection_recall=round(_safe_div(selection_hits, positives), 4),
        frames_scanned=scanned,
        frames_selected=selected,
        reduction_ratio=round(1.0 - _safe_div(selected, scanned), 4),
        cost_usd=round(total_cost, 8),
        cost_per_stream_hour_usd=round(
            total_cost / _safe_div(total_seconds, 3600.0), 6),
        mean_latency_ms=round(
            sum(r.latency_ms for r in results) / max(1, len(results)), 2),
        baseline_precision=round(b_precision, 4),
        baseline_recall=round(b_recall, 4),
        baseline_f1=round(
            _safe_div(2 * b_precision * b_recall, b_precision + b_recall), 4),
        baseline_missed_event_rate=round(_safe_div(b_fn, positives), 4),
        baseline_false_positives=b_fp,
    )
    return metrics, rows, results


def build_dataset(root: Path, n_clips: int = 12, seed: int = 7) -> list[GroundTruth]:
    """Half the clips contain a brief, small, fast-moving event."""

    root.mkdir(parents=True, exist_ok=True)
    truths: list[GroundTruth] = []
    for i in range(n_clips):
        has_event = i % 2 == 0
        start = 6.0 + (i % 4) * 2.5
        events = [(start, start + 2.0)] if has_event else []
        clip_path = root / f"clip_{i:03d}.mp4"
        truths.append(
            make_synthetic_clip(
                clip_path,
                duration_s=20.0,
                fps=25.0,
                events=events,
                seed=seed + i,
            )
        )
    return truths


def main() -> None:
    ap = argparse.ArgumentParser(description="Argus evaluation harness")
    ap.add_argument("--clips", type=int, default=12)
    ap.add_argument("--out", default="var/eval")
    ap.add_argument("--reasoner", default="heuristic")
    args = ap.parse_args()

    out = Path(args.out)
    cfg = PipelineConfig(out_dir=out / "runs")
    pipeline = TriagePipeline(cfg, reasoner_backend=args.reasoner)

    started = time.perf_counter()
    truths = build_dataset(out / "dataset", n_clips=args.clips)
    metrics, rows, results = evaluate(pipeline, truths)
    elapsed = time.perf_counter() - started

    (out / "metrics.json").write_text(
        json.dumps(metrics.to_dict(), indent=2), encoding="utf-8"
    )
    (out / "rows.json").write_text(
        json.dumps(rows, indent=2), encoding="utf-8"
    )
    (out / "summary.json").write_text(
        json.dumps(
            {
                "summary": summarize(results),
                "wall_clock_s": round(elapsed, 2),
                "reasoner": pipeline.reasoner.name,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    print(json.dumps(metrics.to_dict(), indent=2))


if __name__ == "__main__":
    main()