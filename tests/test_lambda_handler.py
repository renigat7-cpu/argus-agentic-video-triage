"""Lambda handler tests: S3 event in, decision JSON back out, no AWS SDK needed."""

from __future__ import annotations

import json
import sys

import pytest

from argus import lambda_handler as lh
from argus.config import PipelineConfig
from argus.pipeline import TriagePipeline


class FakeS3:
    def __init__(self) -> None:
        self.downloads: list[tuple[str, str, str]] = []
        self.uploads: list[dict] = {}
        self.payload: bytes = b""

    def download_file(self, bucket: str, key: str, target: str) -> None:
        self.downloads.append((bucket, key, target))
        with open(target, "wb") as fh:  # noqa: PTH123
            fh.write(self.payload)

    def put_object(self, Bucket: str, Key: str, Body: bytes, ContentType: str = "", **_: object) -> dict:
        self.uploads[(Bucket, Key)] = Body
        return {}


@pytest.fixture(autouse=True)
def _reset_hooks(tmp_path):
    lh.set_s3_client(None)
    lh.set_pipeline(TriagePipeline(PipelineConfig(out_dir=tmp_path / "out")))
    yield
    lh.set_s3_client(None)
    lh.set_pipeline(None)


def _event(key: str = "inbox/clip_000.mp4") -> dict:
    return {"Records": [{"s3": {"bucket": {"name": "argus-in"}, "object": {"key": key}}}]}


def test_handler_triages_an_uploaded_clip(tmp_path):
    from argus.eval import make_synthetic_clip

    clip = make_synthetic_clip(
        tmp_path / "clip.mp4", duration_s=3.0, events=[(1.0, 2.0)], seed=1
    ).clip
    s3 = FakeS3()
    s3.payload = open(clip, "rb").read()  # noqa: SIM115, PTH123
    lh.set_s3_client(s3)

    response = lh.handler(_event(), None)

    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert len(body["results"]) == 1
    result = body["results"][0]
    assert result["decision"] in {"confirm", "escalate", "dismiss"}
    assert result["clip"] == "inbox/clip_000.mp4"

    assert s3.downloads[0][:2] == ("argus-in", "inbox/clip_000.mp4")
    assert result["result_key"].startswith("results/")
    stored = json.loads(s3.uploads[("argus-in", result["result_key"])].decode("utf-8"))
    assert stored["decision"] == result["decision"]


def test_handler_reports_an_undecodable_object_without_failing_the_batch(tmp_path):
    s3 = FakeS3()
    s3.payload = b"not a video at all" * 64
    lh.set_s3_client(s3)

    response = lh.handler(_event("inbox/junk.mp4"), None)

    assert response["statusCode"] == 200
    result = json.loads(response["body"])["results"][0]
    assert result["decision"] == "escalate"
    assert result["rationale"].startswith("decode_error")


def test_handler_without_boto3_still_answers(monkeypatch):
    monkeypatch.setitem(sys.modules, "boto3", None)
    lh.set_s3_client(None)
    response = lh.handler(_event(), None)

    assert response["statusCode"] == 200
    assert "error" in json.loads(response["body"])


def test_malformed_record_does_not_raise():
    lh.set_s3_client(FakeS3())
    response = lh.handler({"Records": [{"s3": {}}]}, None)

    assert response["statusCode"] == 200
    assert json.loads(response["body"])["results"][0]["decision"] == "escalate"
