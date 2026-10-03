"""LLM tool-loop tests: the model picks tools, we feed results back."""

from __future__ import annotations

import json

import httpx

from argus.agent import LLMReasoner
from argus.config import PolicyConfig
from argus.observability import ToolTrace
from argus.policy import Evidence
from argus.signals import FrameSignals


def _evidence(motion: float = 0.5) -> Evidence:
    frames = [
        FrameSignals(
            index=i,
            timestamp_s=i * 0.25,
            motion=motion,
            optical_flow_mag=motion,
            edge_density=0.1,
            saliency=0.35,
            color_anomaly=0.02,
            luma=0.5,
        )
        for i in range(5)
    ]
    return Evidence(frames=frames, clip_duration_s=10.0, fps=25.0, width=640, height=360)


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


def _completion(text: str = "") -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


def _completion_tool_call(name: str, call_id: str) -> dict:
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {"name": name, "arguments": "{}"},
                        }
                    ],
                }
            }
        ]
    }


def _install(monkeypatch, responses: list[dict]) -> list[dict]:
    """Patch ``httpx.post`` with a scripted queue of completions."""

    sent: list[dict] = []
    queue = list(responses)

    def fake_post(
        url: str, headers: dict, json: dict, timeout: float  # noqa: A002
    ) -> httpx.Response:
        sent.append({"url": url, "headers": headers, "body": json})
        return FakeResponse(queue.pop(0))

    monkeypatch.setenv("ARGUS_LLM_KEY", "test-key")
    monkeypatch.setenv("ARGUS_LLM_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setattr("httpx.post", fake_post)
    return sent


def test_llm_reasoner_runs_a_real_tool_loop(monkeypatch):
    sent = _install(
        monkeypatch,
        [
            _completion_tool_call("clip_summary", "call_1"),
            _completion_tool_call("gate_check", "call_2"),
            _completion('{"confidence": 0.88, "rationale": "motion persists across frames"}'),
        ],
    )
    reasoner = LLMReasoner(PolicyConfig())
    trace = ToolTrace()

    decision = reasoner.reason(_evidence(), trace)

    assert decision.decision == "confirm"
    assert decision.confidence == 0.88
    assert reasoner.calls == 3

    names = [c["name"] for c in trace.to_list()]
    assert names.count("llm_step") == 3
    assert "clip_summary" in names and "gate_check" in names
    assert len([n for n in names if n != "llm_step"]) > 1, "several tool calls are traced"

    # Tool results are fed back as role=tool messages keyed by tool_call_id.
    tool_messages = [m for m in sent[-1]["body"]["messages"] if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tool_messages] == ["call_1", "call_2"]
    assert "gates" in json.loads(tool_messages[-1]["content"])["result"]
    assert [f["function"]["name"] for f in sent[0]["body"]["tools"]] == [
        "clip_summary",
        "motion_profile",
        "timeline",
        "gate_check",
    ]


def test_llm_reasoner_falls_back_when_the_endpoint_fails(monkeypatch):
    def boom(*args: object, **kwargs: object) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    monkeypatch.setenv("ARGUS_LLM_KEY", "test-key")
    monkeypatch.setattr("httpx.post", boom)
    trace = ToolTrace()

    decision = LLMReasoner(PolicyConfig()).reason(_evidence(), trace)

    assert decision.decision in {"confirm", "dismiss", "escalate"}
    fallbacks = [c for c in trace.to_list() if c["name"] == "llm_fallback"]
    assert fallbacks and fallbacks[0]["args"]["reason"] == "ConnectError"
    assert any(c["name"] == "gate_check" for c in trace.to_list())


def test_llm_reasoner_falls_back_when_json_is_missing(monkeypatch):
    _install(monkeypatch, [_completion("looks fine to me")])
    trace = ToolTrace()

    decision = LLMReasoner(PolicyConfig()).reason(_evidence(), trace)

    assert any(c["name"] == "llm_fallback" for c in trace.to_list())
    assert decision.decision in {"confirm", "dismiss", "escalate"}


def test_unknown_tool_is_reported_to_the_model_not_raised(monkeypatch):
    sent = _install(
        monkeypatch,
        [
            _completion_tool_call("rm_rf_slash", "call_1"),
            _completion('{"confidence": 0.4, "rationale": "unsure"}'),
        ],
    )
    trace = ToolTrace()

    decision = LLMReasoner(PolicyConfig()).reason(_evidence(), trace)

    tool_message = [m for m in sent[-1]["body"]["messages"] if m["role"] == "tool"][0]
    assert "unknown tool" in tool_message["content"]
    assert decision.decision == "escalate"


def test_step_cap_is_enforced(monkeypatch):
    _install(
        monkeypatch,
        [_completion_tool_call("timeline", f"call_{i}") for i in range(12)],
    )
    trace = ToolTrace()

    decision = LLMReasoner(PolicyConfig()).reason(_evidence(), trace)

    assert len([c for c in trace.to_list() if c["name"] == "llm_step"]) == 5
    assert decision.decision in {"confirm", "dismiss", "escalate"}
