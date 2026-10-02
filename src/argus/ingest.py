"""Clip ingestion and signal extraction."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import cv2
import numpy as np

from .config import SelectionConfig
from .signals import FrameSignals, frame_signals, preprocess


@dataclass(frozen=True)
class ClipMeta:
    path: str
    width: int
    height: int
    fps: float
    n_frames: int
    duration_s: float


def probe(path: str | Path) -> ClipMeta:
    """Read container metadata without decoding frames."""

    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise RuntimeError(f"cannot open video: {path}")
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0) or 25.0
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    cap.release()
    return ClipMeta(
        path=str(path),
        width=w,
        height=h,
        fps=fps,
        n_frames=n,
        duration_s=(n / fps) if fps else 0.0,
    )


def sample_indices(n_frames: int, fps: float, sample_fps: float) -> list[int]:
    """Evenly pick source frame indices for the requested sampling rate."""

    if n_frames <= 0 or fps <= 0 or sample_fps <= 0:
        return []
    step = max(1, int(round(fps / sample_fps)))
    return list(range(0, n_frames, step))


def iter_frames(
    path: str | Path, cfg: SelectionConfig, max_frames: int | None = None
) -> Iterator[tuple[int, float, np.ndarray]]:
    """Yield ``(index, timestamp_s, frame)`` at the configured sampling rate."""

    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise RuntimeError(f"cannot open video: {path}")
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0) or 25.0
    step = max(1, int(round(fps / max(cfg.sample_fps, 1e-6))))
    index = 0
    emitted = 0
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            if index % step == 0:
                yield index, index / fps, frame
                emitted += 1
                if max_frames and emitted >= max_frames:
                    break
            index += 1
    finally:
        cap.release()


def extract_signals(
    path: str | Path, cfg: SelectionConfig, max_frames: int | None = None
) -> tuple[list[FrameSignals], ClipMeta]:
    """Decode a clip into per-frame signals at the configured sampling rate."""

    meta = probe(path)
    signals: list[FrameSignals] = []
    prev_gray: np.ndarray | None = None
    baseline: np.ndarray | None = None

    for index, timestamp, frame in iter_frames(path, cfg, max_frames=max_frames):
        gray, hsv = preprocess(frame, cfg.short_side)
        sig, baseline = frame_signals(index, timestamp, prev_gray, gray, hsv, baseline)
        signals.append(sig)
        prev_gray = gray

    return signals, meta