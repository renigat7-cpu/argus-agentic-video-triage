"""AWS Lambda entry point: triage a clip dropped into S3.

Triggered by an S3 object-created event. One clip in, one decision JSON back out
under the results prefix, plus the same escalation/decision logging the
long-running service does. Deploying this is the entire "AWS" story for Argus:
S3 in, Lambda (this file) for triage, S3 + CloudWatch out.

``boto3`` is imported lazily and the S3 client is injectable
(:func:`set_s3_client`), so the handler is unit-testable with a fake and the
module imports fine on a machine with no AWS SDK at all.
"""

from __future__ import annotations

import json
import os
import tempfile
import urllib.parse
from pathlib import Path
from typing import Any

from .config import PipelineConfig
from .pipeline import TriagePipeline

RESULTS_PREFIX = "results/"
_S3_OVERRIDE: Any | None = None
_PIPELINE: TriagePipeline | None = None


def set_s3_client(client: Any | None) -> None:
    """Inject a fake (or real) S3 client. Used by tests."""

    global _S3_OVERRIDE
    _S3_OVERRIDE = client


def get_s3_client() -> Any:
    if _S3_OVERRIDE is not None:
        return _S3_OVERRIDE
    import boto3

    return boto3.client("s3")


def set_pipeline(pipeline: TriagePipeline | None) -> None:
    """Inject a pipeline (tests), or clear the cached one."""

    global _PIPELINE
    _PIPELINE = pipeline


def get_pipeline() -> TriagePipeline:
    """Cached pipeline; writes land in ``/tmp`` because Lambda's bundle is read-only."""

    global _PIPELINE
    if _PIPELINE is None:
        out_dir = Path(os.environ.get("ARGUS_OUT_DIR", "/tmp/argus"))
        cfg = PipelineConfig(out_dir=out_dir)
        _PIPELINE = TriagePipeline(
            cfg, reasoner_backend=os.environ.get("ARGUS_REASONER", "heuristic")
        )
    return _PIPELINE


def _results_prefix() -> str:
    return os.environ.get("ARGUS_RESULTS_PREFIX", RESULTS_PREFIX)


def _process(bucket: str, key: str, location: str, s3: Any) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="argus-lambda-") as tmpdir:
        local = Path(tmpdir) / Path(urllib.parse.unquote_plus(key)).name
        s3.download_file(bucket, key, str(local))
        result = get_pipeline().triage(local, location=location)
        payload = result.to_dict()
        payload["clip"] = key
        out_key = f"{_results_prefix()}{result.run_id}.json"
        try:
            s3.put_object(
                Bucket=bucket,
                Key=out_key,
                Body=json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"),
                ContentType="application/json",
            )
            payload["result_key"] = out_key
        except Exception as exc:  # noqa: BLE001 — a lost write must not fail the batch
            payload["result_key"] = ""
            payload["upload_error"] = f"{type(exc).__name__}: {exc}"
        return payload


def handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """S3 event -> decisions. Always answers 200 with per-record outcomes."""

    results: list[dict[str, Any]] = []
    try:
        s3 = get_s3_client()
    except Exception as exc:  # noqa: BLE001 — no SDK, no credentials
        return {
            "statusCode": 200,
            "body": json.dumps(
                {"error": f"{type(exc).__name__}: {exc}", "results": []},
            ),
        }

    for record in (event or {}).get("Records", []) or []:
        payload = record.get("s3", {}) if isinstance(record, dict) else {}
        bucket = str((payload.get("bucket") or {}).get("name") or "")
        key = str((payload.get("object") or {}).get("key") or "")
        if not bucket or not key:
            results.append({"clip": key, "decision": "escalate", "error": "malformed s3 record"})
            continue
        try:
            results.append(_process(bucket, key, bucket, s3))
        except Exception as exc:  # noqa: BLE001
            results.append(
                {
                    "clip": key,
                    "decision": "escalate",
                    "confidence": 0.0,
                    "rationale": f"lambda_error: {exc}",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )

    return {"statusCode": 200, "body": json.dumps({"results": results}, default=str)}


__all__ = ["handler", "get_pipeline", "get_s3_client", "set_pipeline", "set_s3_client"]
