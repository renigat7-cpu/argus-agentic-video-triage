"""Per-frame evidence extraction built on OpenCV 5.

Each signal is deliberately cheap: the whole point of Argus is that ranking
frames must cost far less than understanding them. Signals are computed on a
downscaled grayscale/HSV pair so the per-frame cost stays flat as resolution
grows.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any

import cv2
import numpy as np


@dataclass(frozen=True)
class FrameSignals:
    """Cheap, resolution-independent descriptors for a single frame."""

    index: int
    timestamp_s: float
    motion: float
    """Mean absolute inter-frame difference, normalised to 0..1."""

    optical_flow_mag: float
    """Mean Farneback flow magnitude, normalised to 0..1."""

    edge_density: float
    """Canny edge pixel ratio."""

    saliency: float
    """Peakiness of the saliency map, 0..1. Zero when unavailable."""

    color_anomaly: float
    """Bhattacharyya-style distance from the running colour baseline."""

    luma: float
    """Mean luminance, used to detect washed-out or night frames."""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _normalise(x: np.ndarray) -> float:
    """Scale an array to 0..1 against its own robust maximum."""

    if x.size == 0:
        return 0.0
    peak = float(np.percentile(x, 99.0))
    if peak <= 1e-6:
        return 0.0
    return float(np.clip(x.mean() / peak, 0.0, 1.0))


_SALIENT = None


def _saliency_map(gray: np.ndarray) -> np.ndarray | None:
    """Static saliency map for one frame.

    ``cv2.saliency`` ships in opencv-contrib. If it is unavailable we return
    ``None`` and the caller drops the term rather than silently scoring zero
    against a real signal.
    """

    global _SALIENT
    if not hasattr(cv2, "saliency"):
        return None
    try:
        if _SALIENT is None:
            _SALIENT = cv2.saliency.StaticSaliencySpectralResidual_create()
        out = _SALIENT.computeSaliency(gray)
        _, sal = out if isinstance(out, tuple) else (True, out)
        return np.asarray(sal, dtype=np.float32)
    except Exception:
        return None


def saliency_score(gray: np.ndarray) -> float:
    """Peakiness of the saliency map: how strongly one region stands out.

    We use the high percentile rather than the mean because a real event is
    usually a small object; averaging over a mostly-empty map would erase it.
    """

    sal = _saliency_map(gray)
    if sal is None or sal.size == 0:
        return 0.0
    return float(np.clip(np.percentile(sal, 99.5), 0.0, 1.0))


def motion_score(prev_gray: np.ndarray, gray: np.ndarray) -> float:
    """Fast inter-frame difference signal."""

    diff = cv2.absdiff(prev_gray, gray)
    diff = cv2.GaussianBlur(diff, (5, 5), 0)
    return _normalise(diff)


def optical_flow_mag(prev_gray: np.ndarray, gray: np.ndarray) -> float:
    """Farneback dense flow, coarse but far more sensitive than absdiff.

    This is the signal that rescues slow-onset events, which pure frame
    differencing tends to miss.
    """

    flow = cv2.calcOpticalFlowFarneback(
        prev_gray, gray, None,
        pyr_scale=0.5, levels=2, winsize=9, iterations=2,
        poly_n=5, poly_sigma=1.1, flags=0,
    )
    mag = cv2.magnitude(flow[..., 0], flow[..., 1])
    return _normalise(mag)


def edge_density(gray: np.ndarray) -> float:
    """Canny edge ratio: texture and structure, cheap and stable."""

    edges = cv2.Canny(gray, 60, 180)
    return float(np.count_nonzero(edges) / max(1, edges.size))


def color_anomaly(hsv: np.ndarray, baseline: np.ndarray | None) -> tuple[float, np.ndarray]:
    """Distance of a frame's hue/sat histogram from a running baseline.

    Returns the anomaly and the updated baseline so callers can stream frames
    without holding the whole clip in memory.
    """

    hist = cv2.calcHist([hsv], [0, 1], None, [24, 16], [0, 180, 0, 256])
    cv2.normalize(hist, hist, 0, 1, cv2.NORM_MINMAX)
    if baseline is None:
        return 0.0, hist
    distance = cv2.compareHist(baseline, hist, cv2.HISTCMP_BHATTACHARYYA)
    blended = 0.95 * baseline + 0.05 * hist
    cv2.normalize(blended, blended, 0, 1, cv2.NORM_MINMAX)
    return float(distance), blended


def preprocess(frame: np.ndarray, short_side: int) -> tuple[np.ndarray, np.ndarray]:
    """Return (gray, hsv) downscaled so the short side equals ``short_side``."""

    h, w = frame.shape[:2]
    scale = short_side / float(min(h, w))
    if scale < 1.0:
        frame = cv2.resize(frame, (max(1, int(w * scale)), max(1, int(h * scale))),
                           interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    return gray, hsv


def frame_signals(
    index: int,
    timestamp_s: float,
    prev_gray: np.ndarray | None,
    gray: np.ndarray,
    hsv: np.ndarray,
    baseline: np.ndarray | None,
) -> tuple[FrameSignals, np.ndarray]:
    """Compute the full signal bundle for one frame.

    Returns the signals and the updated colour baseline.
    """

    if prev_gray is None:
        motion = 0.0
        flow = 0.0
    else:
        motion = motion_score(prev_gray, gray)
        flow = optical_flow_mag(prev_gray, gray)
    anomaly, baseline = color_anomaly(hsv, baseline)
    signals = FrameSignals(
        index=index,
        timestamp_s=round(float(timestamp_s), 3),
        motion=motion,
        optical_flow_mag=flow,
        edge_density=edge_density(gray),
        saliency=saliency_score(gray),
        color_anomaly=anomaly,
        luma=float(gray.mean() / 255.0),
    )
    return signals, baseline