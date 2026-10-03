"""Optional AWS delivery for decision records.

The decision log on local disk is the source of truth; everything in this module
is a *mirror* of it. That is why :meth:`AwsSink.put_decision` can never raise:
losing a CloudWatch point is not a reason to fail a triage run, and a sink that
crashes the pipeline would be strictly worse than no sink at all.

``boto3`` is imported lazily and the module is importable without it, so the
test suite and the heuristic evaluation path never need AWS installed.
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any

TRUTHY = frozenset({"1", "true", "yes", "on", "s3", "enabled"})
"""``ARGUS_AWS_SINK`` values that switch the mirror on. ``s3`` is what the
container/Lambda task definitions in ``infra/terraform`` set."""

DEFAULT_PREFIX = "decisions/"


def sink_enabled() -> bool:
    """``ARGUS_AWS_SINK`` set to a truthy string, case-insensitively."""

    return os.environ.get("ARGUS_AWS_SINK", "").strip().lower() in TRUTHY


def boto3_available() -> bool:
    """True when ``boto3`` can actually be imported."""

    try:
        import boto3  # noqa: F401

        return True
    except Exception:
        return False


def _region() -> str:
    return os.environ.get("ARGUS_AWS_REGION", "") or os.environ.get("AWS_REGION", "")


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


class AwsSink:
    """Ship one decision record to S3 and CloudWatch, best effort.

    Clients are dependency-injected (``client``/``s3``/``cloudwatch``) so the
    behaviour above can be tested without AWS credentials or network access.
    """

    def __init__(
        self,
        client: Any | None = None,
        s3: Any | None = None,
        cloudwatch: Any | None = None,
        enabled: bool | None = None,
        bucket: str | None = None,
        prefix: str | None = None,
        namespace: str | None = None,
        region: str | None = None,
    ) -> None:
        self._s3 = s3 if s3 is not None else client
        self._cloudwatch = cloudwatch if cloudwatch is not None else client
        self._injected = self._s3 is not None or self._cloudwatch is not None
        self.enabled = sink_enabled() if enabled is None else bool(enabled)
        self.bucket = bucket if bucket is not None else os.environ.get("ARGUS_S3_BUCKET", "")
        self.prefix = prefix if prefix is not None else os.environ.get(
            "ARGUS_S3_PREFIX", DEFAULT_PREFIX
        )
        self.namespace = namespace if namespace is not None else os.environ.get(
            "ARGUS_CW_NAMESPACE", ""
        )
        self.region = region if region is not None else _region()
        self.errors = 0

    # ------------------------------------------------------------------ setup

    @staticmethod
    def _boto3() -> Any:
        import boto3

        return boto3

    @property
    def available(self) -> bool:
        """Enabled *and* the SDK is importable."""

        return self.enabled and boto3_available()

    def s3_client(self) -> Any | None:
        if self._s3 is not None:
            return self._s3
        try:
            kwargs = {"region_name": self.region} if self.region else {}
            return self._boto3().client("s3", **kwargs)
        except Exception as exc:
            self._fail("s3_client", exc)
            return None

    def cloudwatch_client(self) -> Any | None:
        if self._cloudwatch is not None:
            return self._cloudwatch
        try:
            kwargs = {"region_name": self.region} if self.region else {}
            return self._boto3().client("cloudwatch", **kwargs)
        except Exception as exc:
            self._fail("cloudwatch_client", exc)
            return None

    def _fail(self, what: str, exc: BaseException) -> None:
        self.errors += 1
        print(
            f"argus.aws_store: {what} failed ({type(exc).__name__}: {exc})",
            file=sys.stderr,
        )

    # ------------------------------------------------------------------- keys

    def key_for(self, run_id: str) -> str:
        """``<prefix><YYYY-MM-DD>/<run_id>.json`` - one object per decision."""

        return f"{self.prefix}{_today()}/{run_id}.json"

    # ------------------------------------------------------------------- api

    def put_decision(self, record: dict[str, Any]) -> str | None:
        """Upload one decision JSON object and emit metrics. Never raises."""

        if not self.enabled or not isinstance(record, dict):
            return None

        key = self.key_for(str(record.get("run_id") or "unknown"))

        if self.bucket:
            try:
                client = self.s3_client()
                if client is None:
                    return None
                body = json.dumps(record, ensure_ascii=False, default=str)
                client.put_object(
                    Bucket=self.bucket,
                    Key=key,
                    Body=body.encode("utf-8"),
                    ContentType="application/json",
                )
            except Exception as exc:  # noqa: BLE001 — telemetry must not break triage
                self._fail(f"put_object {key}", exc)

        if self.namespace:
            self.put_metric(record)
        return key

    def put_metric(self, record: dict[str, Any]) -> bool:
        """Publish Decisions/Confidence/LatencyMs for one decision. Never raises."""

        if not self.enabled or not self.namespace or not isinstance(record, dict):
            return False
        try:
            decision = str(record.get("decision", "unknown"))
            dimensions = [{"Name": "Decision", "Value": decision}]
            data = [
                {"MetricName": "Decisions", "Value": 1.0, "Unit": "Count",
                 "Dimensions": dimensions},
                {"MetricName": "Confidence", "Value": float(record.get("confidence", 0.0)),
                 "Unit": "None", "Dimensions": dimensions},
                {"MetricName": "LatencyMs", "Value": float(record.get("latency_ms", 0.0)),
                 "Unit": "Milliseconds", "Dimensions": dimensions},
            ]
            client = self.cloudwatch_client()
            if client is None:
                return False
            client.put_metric_data(Namespace=self.namespace, MetricData=data)
            return True
        except Exception as exc:  # noqa: BLE001
            self._fail("put_metric_data", exc)
            return False


__all__ = ["AwsSink", "boto3_available", "sink_enabled"]
