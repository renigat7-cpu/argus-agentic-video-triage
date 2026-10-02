"""FastAPI service: upload a clip, get a triage decision with full trace.

This is the "working web endpoint" the submission requires, and it doubles as
the demo surface: every response carries the decision, the gate results, the
tool calls, and the cost, so a judge can audit a decision without logs.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import load_config
from .ingest import probe
from .pipeline import TriagePipeline, summarize

WEB_DIR = Path(__file__).resolve().parent.parent.parent / "web"

app = FastAPI(
    title="Argus - Agentic Video Triage",
    version="0.1.0",
    description=(
        "OpenCV 5 frame selection plus a tool-using agent that verifies, dismisses, "
        "or escalates candidate events, with a decision trace attached to every answer."
    ),
)

_cfg = load_config(os.environ.get("ARGUS_CONFIG"))
_pipeline = TriagePipeline(_cfg, reasoner_backend=os.environ.get("ARGUS_REASONER", "heuristic"))

UPLOAD_DIR = Path(os.environ.get("ARGUS_UPLOAD_DIR", "./var/uploads"))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

if WEB_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    return {"ok": True, "reasoner": _pipeline.reasoner.name, "opencv": _opencv_version()}


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    page = WEB_DIR / "templates" / "index.html"
    if page.is_file():
        return HTMLResponse(page.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>Argus</h1><p>UI not installed.</p>")


@app.post("/api/triage")
async def triage(
    file: UploadFile = File(...),
    location: str = Form("unknown"),
    reasoner: str | None = Form(None),
) -> JSONResponse:
    """Triage an uploaded clip and return the decision plus its full trace."""

    suffix = Path(file.filename or "clip.mp4").suffix or ".mp4"
    tmp = Path(tempfile.mkdtemp(prefix="argus-")) / f"clip{suffix}"
    try:
        with tmp.open("wb") as fh:
            shutil.copyfileobj(file.file, fh)

        try:
            meta = probe(tmp)
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        pipe = (
            _pipeline
            if not reasoner
            else TriagePipeline(_cfg, reasoner_backend=reasoner)
        )
        result = pipe.triage(tmp, location=location)
        payload = result.to_dict()
        payload["clip"] = file.filename
        payload["meta"] = {
            "width": meta.width,
            "height": meta.height,
            "fps": meta.fps,
            "n_frames": meta.n_frames,
            "duration_s": round(meta.duration_s, 3),
        }
        return JSONResponse(payload)
    finally:
        shutil.rmtree(tmp.parent, ignore_errors=True)


@app.post("/api/triage-batch")
async def triage_batch(
    files: list[UploadFile] = File(...),
    location: str = Form("unknown"),
) -> JSONResponse:
    """Triage several clips and return the batch summary plus per-clip rows."""

    results = []
    for upload in files:
        suffix = Path(upload.filename or "clip.mp4").suffix or ".mp4"
        tmp = Path(tempfile.mkdtemp(prefix="argus-")) / f"clip{suffix}"
        try:
            with tmp.open("wb") as fh:
                shutil.copyfileobj(upload.file, fh)
            row = _pipeline.triage(tmp, location=location).to_dict()
            row["clip"] = upload.filename
            results.append(row)
        finally:
            shutil.rmtree(tmp.parent, ignore_errors=True)
    return JSONResponse({"summary": summarize_all(results), "results": results})


@app.get("/api/decisions")
def decisions(limit: int = 50) -> dict[str, Any]:
    """Recent decisions from the append-only decision log."""

    rows = _pipeline.recorder.decisions()
    return {"count": len(rows), "decisions": rows[-limit:]}


def summarize_all(rows: list[dict[str, Any]]) -> dict[str, Any]:
    from .pipeline import TriageResult

    objects = [
        TriageResult(
            run_id=r.get("run_id", ""),
            clip=r.get("clip", ""),
            decision=r.get("decision", ""),
            confidence=r.get("confidence", 0.0),
            confidence_label=r.get("confidence_label", ""),
            rationale=r.get("rationale", ""),
            n_frames_scanned=r.get("n_frames_scanned", 0),
            n_frames_selected=r.get("n_frames_selected", 0),
            cost_usd=r.get("cost_usd", 0.0),
            latency_ms=r.get("latency_ms", 0.0),
        )
        for r in rows
    ]
    return summarize(objects)


def _opencv_version() -> str:
    try:
        import cv2

        return cv2.__version__
    except Exception:
        return "unavailable"