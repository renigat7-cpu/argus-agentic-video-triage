"""Typed configuration for the Argus triage pipeline.

Every threshold that influences a decision lives here so it is auditable and
reproducible. Nothing in the pipeline reads an undeclared magic number.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

Decision = Literal["confirm", "dismiss", "escalate"]


@dataclass(frozen=True)
class SelectionConfig:
    """Frame-selection stage: which frames are worth expensive analysis."""

    sample_fps: float = 4.0
    """Frames per second pulled from the source before scoring."""

    short_side: int = 320
    """Frames are downscaled to this short side for cheap scoring."""

    top_k: int = 24
    """Maximum frames forwarded to the reasoning stage per clip."""

    motion_weight: float = 1.0
    edge_weight: float = 0.45
    saliency_weight: float = 0.55
    color_weight: float = 0.30

    diversity_ms: int = 900
    """Minimum gap between two selected frames, enforces temporal spread."""

    motion_percentile: float = 90.0
    """Motion below this percentile is treated as background noise."""

    min_score: float = 0.08
    """Frames scoring below this are dropped outright."""


@dataclass(frozen=True)
class PolicyConfig:
    """Policy thresholds for the reasoning stage."""

    confirm_at: float = 0.80
    """At or above this confidence a candidate event is auto-confirmed."""

    dismiss_at: float = 0.35
    """At or below this confidence a candidate is auto-dismissed."""

    escalate_below: float = 0.80
    """Anything below confirm_at and above dismiss_at goes to a human."""

    max_evidence_frames: int = 5
    """Evidence bundle size handed to the reasoner."""

    require_motion_for_confirm: bool = True
    """Refuse to auto-confirm an event on a static scene."""


@dataclass(frozen=True)
class CostModel:
    """Explicit cost accounting so evaluation is comparable across configs."""

    reasoning_usd_per_call: float = 0.0025
    select_cpu_usd_per_frame: float = 0.0000021
    store_usd_per_frame: float = 0.0000009

    def reasoning_call_cost(self, n: int) -> float:
        return self.reasoning_usd_per_call * max(0, n)

    def frame_cost(self, n: int) -> float:
        return (self.select_cpu_usd_per_frame + self.store_usd_per_frame) * max(0, n)


@dataclass(frozen=True)
class PipelineConfig:
    """Top-level pipeline configuration."""

    selection: SelectionConfig = field(default_factory=SelectionConfig)
    policy: PolicyConfig = field(default_factory=PolicyConfig)
    cost: CostModel = field(default_factory=CostModel)

    out_dir: Path = field(
        default_factory=lambda: Path(os.environ.get("ARGUS_OUT_DIR", "./var"))
    )
    decision_log: str = "decisions.jsonl"
    run_log: str = "runs.jsonl"

    def to_json(self) -> str:
        payload = asdict(self)
        payload["out_dir"] = str(self.out_dir)
        return json.dumps(payload, indent=2, sort_keys=True)

    def ensure_dirs(self) -> None:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        (self.out_dir / "evidence").mkdir(parents=True, exist_ok=True)


def load_config(path: str | Path | None = None) -> PipelineConfig:
    """Build a config from JSON overrides, falling back to defaults."""

    if not path:
        return PipelineConfig()
    raw: dict[str, Any] = json.loads(Path(path).read_text(encoding="utf-8"))
    sel = SelectionConfig(**raw.get("selection", {}))
    pol = PolicyConfig(**raw.get("policy", {}))
    cost = CostModel(**raw.get("cost", {}))
    rest = {k: v for k, v in raw.items() if k not in ("selection", "policy", "cost")}
    if "out_dir" in rest:
        rest["out_dir"] = Path(rest["out_dir"])
    return PipelineConfig(selection=sel, policy=pol, cost=cost, **rest)