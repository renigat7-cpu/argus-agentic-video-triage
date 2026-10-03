"""AWS sink tests: telemetry is best effort and must never break a decision."""

from __future__ import annotations

import json
import time

from argus.aws_store import AwsSink, boto3_available, sink_enabled


class FakeS3:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}

    def put_object(
        self, Bucket: str, Key: str, Body: bytes, ContentType: str = "", **_: object
    ) -> dict:
        self.objects[(Bucket, Key)] = Body
        return {"ETag": "fake"}


class FakeCloudWatch:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def put_metric_data(self, Namespace: str, MetricData: list[dict]) -> dict:
        self.calls.append({"Namespace": Namespace, "MetricData": MetricData})
        return {}


class ExplodingClient:
    def put_object(self, **_: object) -> dict:
        raise RuntimeError("s3 is down")

    def put_metric_data(self, **_: object) -> dict:
        raise RuntimeError("cloudwatch is down")


def _record() -> dict:
    return {
        "run_id": "abc123",
        "clip": "clip.mp4",
        "decision": "escalate",
        "confidence": 0.55,
        "latency_ms": 120.5,
    }


def test_sink_disabled_is_a_noop(monkeypatch):
    monkeypatch.delenv("ARGUS_AWS_SINK", raising=False)
    assert sink_enabled() is False

    s3, cw = FakeS3(), FakeCloudWatch()
    sink = AwsSink(s3=s3, cloudwatch=cw)

    assert sink.put_decision(_record()) is None
    assert s3.objects == {}
    assert cw.calls == []
    assert sink.available is False


def test_sink_enabled_uploads_and_emits_metrics(monkeypatch):
    monkeypatch.setenv("ARGUS_AWS_SINK", "TRUE")
    monkeypatch.setenv("ARGUS_S3_BUCKET", "argus-bucket")
    monkeypatch.setenv("ARGUS_S3_PREFIX", "decisions/")
    monkeypatch.setenv("ARGUS_CW_NAMESPACE", "Argus")

    assert sink_enabled() is True
    s3, cw = FakeS3(), FakeCloudWatch()
    sink = AwsSink(s3=s3, cloudwatch=cw)

    key = sink.put_decision(_record())
    today = time.strftime("%Y-%m-%d", time.gmtime())
    assert key == f"decisions/{today}/abc123.json"
    assert (("argus-bucket", key)) in s3.objects

    body = json.loads(s3.objects[("argus-bucket", key)].decode("utf-8"))
    assert body["decision"] == "escalate"

    assert len(cw.calls) == 1
    data = cw.calls[0]["MetricData"]
    assert cw.calls[0]["Namespace"] == "Argus"
    names = {m["MetricName"]: m["Value"] for m in data}
    assert names["Decisions"] == 1.0
    assert names["Confidence"] == 0.55
    assert names["LatencyMs"] == 120.5
    assert all(m["Dimensions"] == [{"Name": "Decision", "Value": "escalate"}] for m in data)


def test_sink_default_prefix_when_unset(monkeypatch):
    monkeypatch.setenv("ARGUS_AWS_SINK", "yes")
    monkeypatch.delenv("ARGUS_S3_PREFIX", raising=False)
    s3 = FakeS3()
    sink = AwsSink(s3=s3, bucket="b")
    assert sink.prefix == "decisions/"
    assert sink.put_decision(_record()).startswith("decisions/")


def test_sink_never_raises_when_client_fails(monkeypatch, capsys):
    monkeypatch.setenv("ARGUS_AWS_SINK", "1")
    monkeypatch.setenv("ARGUS_S3_BUCKET", "argus-bucket")
    monkeypatch.setenv("ARGUS_CW_NAMESPACE", "Argus")

    sink = AwsSink(client=ExplodingClient())

    assert sink.put_decision(_record()) is not None
    assert sink.put_metric(_record()) is False
    assert sink.errors >= 2
    assert "aws_store" in capsys.readouterr().err


def test_sink_without_boto3_is_not_available(monkeypatch):
    monkeypatch.setenv("ARGUS_AWS_SINK", "1")
    sink = AwsSink()
    # The SDK is optional: without it the sink reports unavailable but never
    # raises at import time, which is what keeps evaluation reproducible.
    assert sink.available is boto3_available()
    if not boto3_available():
        assert sink.available is False
