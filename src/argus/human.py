"""Human-in-the-loop: escalation queue and reviewer verdicts.

"Escalate" is only a real behaviour if something happens next. This module makes
that something concrete and durable: every escalation is appended to
``escalations.jsonl``, every reviewer answer to ``verdicts.jsonl``, and the queue
resolves to the latest state per ``run_id`` so a verdict supersedes the open
escalation it answers.

Two append-only files, no database, no hidden state: a reviewer queue can be
replayed after the fact to audit what the system asked and what humans said.
"""

from __future__ import annotations

import json
import time
from typing import Any, Iterable

from .config import PipelineConfig

VERDICTS: tuple[str, ...] = ("approve", "reject", "needs_more")

ESCALATIONS_FILE = "escalations.jsonl"
VERDICTS_FILE = "verdicts.jsonl"


def _utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _read_rows(path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


class EscalationQueue:
    """Append-only escalation queue with reviewer verdicts."""

    def __init__(self, cfg: PipelineConfig) -> None:
        cfg.ensure_dirs()
        self.cfg = cfg
        self.escalations_path = cfg.out_dir / ESCALATIONS_FILE
        self.verdicts_path = cfg.out_dir / VERDICTS_FILE

    # ------------------------------------------------------------- escalation

    def enqueue(self, record: dict[str, Any]) -> dict[str, Any]:
        """Append an open escalation for one decision and return the stored row."""

        row = {
            "timestamp": _utc(),
            "run_id": str(record.get("run_id", "")),
            "clip": str(record.get("clip", "")),
            "confidence": float(record.get("confidence", 0.0) or 0.0),
            "confidence_label": str(record.get("confidence_label", "")),
            "rationale": str(record.get("rationale", "")),
            "status": "open",
        }
        self._append(self.escalations_path, row)
        return row

    # --------------------------------------------------------------- verdicts

    def verdict(
        self, run_id: str, verdict: str, reviewer: str = "unknown", note: str = ""
    ) -> dict[str, Any]:
        """Record a reviewer's answer to an escalation. Raises on a bad verdict."""

        if verdict not in VERDICTS:
            raise ValueError(f"unknown verdict {verdict!r}; expected one of {VERDICTS}")
        row = {
            "timestamp": _utc(),
            "run_id": str(run_id),
            "verdict": verdict,
            "status": verdict,
            "reviewer": reviewer or "unknown",
            "note": note,
        }
        self._append(self.verdicts_path, row)
        return row

    # ------------------------------------------------------------------ reads

    def state(self) -> dict[str, dict[str, Any]]:
        """Latest state per ``run_id``; a verdict supersedes the open escalation."""

        latest: dict[str, dict[str, Any]] = {}
        for row in _read_rows(self.escalations_path):
            run_id = row.get("run_id") or ""
            if not run_id:
                continue
            latest[run_id] = {**latest.get(run_id, {}), **row}
        for row in _read_rows(self.verdicts_path):
            run_id = row.get("run_id") or ""
            if not run_id:
                continue
            latest[run_id] = {**latest.get(run_id, {}), **row}
        return latest

    def list(self, status: str | None = "open") -> list[dict[str, Any]]:
        """All escalations, optionally filtered by their latest status."""

        rows = list(self.state().values())
        if status is None:
            return rows
        return [r for r in rows if r.get("status") == status]

    def count(self, status: str | None = "open") -> int:
        return len(self.list(status))

    # ------------------------------------------------------------------ utils

    @staticmethod
    def _append(path, row: dict[str, Any]) -> None:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")

    def rows(self) -> Iterable[dict[str, Any]]:
        return _read_rows(self.escalations_path)


__all__ = ["ESCALATIONS_FILE", "VERDICTS", "VERDICTS_FILE", "EscalationQueue"]
