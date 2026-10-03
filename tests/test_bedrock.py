"""Bedrock tests: a real tool loop, and a heuristic fallback that always fires."""

from __future__ import annotations

from copy import deepcopy

from argus.bedrock import DEFAULT_MODEL, BedrockReasoner, bedrock_model
from argus.config import PolicyConfig
from argus.observability import ToolTrace
from argus.policy import Evidence, PolicyDecision
from argus.signals import FrameSignals


def _evidence(motion: float = 0.6) -> Evidence:
    frames = [
        FrameSignals(
            index=i,
            timestamp_s=i * 0.25,
            motion=motion,
            optical_flow_mag=motion,
            edge_density=0.1,
            saliency=0.4,
            color_anomaly=0.02,
            luma=0.5,
        )
        for i in range(5)
    ]
    return Evidence(frames=frames, clip_duration_s=10.0, fps=25.0, width=640, height=360)


class FakeBedrock:
    """Replays scripted Converse responses and records the messages it saw."""

    def __init__(self, responses: list[dict]) -> None:
        self.responses = list(responses)
        self.seen: list[dict] = []

    def converse(self, modelId: str, messages: list[dict], toolConfig: dict, **_: object) -> dict:
        # Snapshot: the driver mutates one shared message list in place.
        self.seen.append(
            {"modelId": modelId, "messages": deepcopy(messages), "toolConfig": toolConfig}
        )
        if not self.responses:
            raise AssertionError("Bedrock called more times than the test scripted")
        return self.responses.pop(0)


class ExplodingBedrock:
    def converse(self, **_: object) -> dict:
        raise RuntimeError("AccessDenied: bedrock is not enabled here")


def _tool_use(name: str, tool_use_id: str = "t1") -> dict:
    return {
        "output": {
            "message": {
                "role": "assistant",
                "content": [{"toolUse": {"toolUseId": tool_use_id, "name": name, "input": {}}}],
            }
        },
        "stopReason": "tool_use",
    }


def _text(text: str) -> dict:
    return {
        "output": {
            "message": {"role": "assistant", "content": [{"text": text}]}
        },
        "stopReason": "end_turn",
    }


def test_bedrock_runs_the_tool_loop_and_returns_a_decision():
    client = FakeBedrock(
        [
            _tool_use("clip_summary"),
            _tool_use("gate_check", "t2"),
            _text('{"confidence": 0.93, "rationale": "sustained motion with a salient subject"}'),
        ]
    )
    reasoner = BedrockReasoner(PolicyConfig(), client=client)
    trace = ToolTrace()

    decision = reasoner.reason(_evidence(), trace)

    assert isinstance(decision, PolicyDecision)
    assert decision.decision == "confirm"
    assert decision.confidence == 0.93
    assert reasoner.name == "bedrock-tools-v1"
    assert reasoner.calls == 3

    names = [c["name"] for c in trace.to_list()]
    assert "bedrock_step" in names
    assert "clip_summary" in names
    assert "gate_check" in names

    # The tool result really was fed back to the model, in a toolResult block.
    last = client.seen[-1]["messages"][-1]
    assert last["role"] == "user"
    assert last["content"][0]["toolResult"]["toolUseId"] == "t2"
    names_offered = [t["toolSpec"]["name"] for t in client.seen[0]["toolConfig"]["tools"]]
    assert names_offered == ["clip_summary", "motion_profile", "timeline", "gate_check"]


def test_bedrock_falls_back_to_heuristic_when_the_model_fails():
    reasoner = BedrockReasoner(PolicyConfig(), client=ExplodingBedrock())
    trace = ToolTrace()

    decision = reasoner.reason(_evidence(), trace)

    assert decision.decision in {"confirm", "dismiss", "escalate"}
    calls = trace.to_list()
    fallbacks = [c for c in calls if c["name"] == "bedrock_fallback"]
    assert fallbacks, "a fallback must be visible in the decision trace"
    assert fallbacks[0]["args"]["reason"] == "RuntimeError"
    assert any(c["name"] == "gate_check" for c in calls), "heuristic tools still ran"


def test_bedrock_falls_back_when_the_answer_is_not_json():
    client = FakeBedrock([_text("I think it is probably fine?")])
    reasoner = BedrockReasoner(PolicyConfig(), client=client)
    trace = ToolTrace()

    decision = reasoner.reason(_evidence(), trace)

    assert decision.decision in {"confirm", "dismiss", "escalate"}
    assert any(c["name"] == "bedrock_fallback" for c in trace.to_list())


def test_bedrock_step_cap_stops_a_runaway_loop():
    client = FakeBedrock([_tool_use("timeline", f"t{i}") for i in range(10)])
    reasoner = BedrockReasoner(PolicyConfig(), client=client)
    trace = ToolTrace()

    decision = reasoner.reason(_evidence(), trace)

    steps = [c for c in trace.to_list() if c["name"] == "bedrock_step"]
    assert len(steps) == 5, "ARGUS_AGENT_MAX_STEPS defaults to 5"
    assert reasoner.calls == 5
    assert decision.decision in {"confirm", "dismiss", "escalate"}


def test_bedrock_model_id_is_configurable(monkeypatch):
    monkeypatch.delenv("ARGUS_BEDROCK_MODEL", raising=False)
    assert bedrock_model() == DEFAULT_MODEL
    monkeypatch.setenv("ARGUS_BEDROCK_MODEL", "anthropic.claude-3-haiku-20240307-v1:0")
    assert BedrockReasoner(PolicyConfig(), client=FakeBedrock([])).model == (
        "anthropic.claude-3-haiku-20240307-v1:0"
    )


def test_build_reasoner_selects_bedrock(capsys):
    from argus.agent import build_reasoner

    assert build_reasoner(PolicyConfig(), "bedrock").name == "bedrock-tools-v1"

    unknown = build_reasoner(PolicyConfig(), "nonsense")
    assert unknown.name == "heuristic-v1"
    assert "unknown reasoner backend" in capsys.readouterr().err
