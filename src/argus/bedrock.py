"""Amazon Bedrock reasoning backend.

Same contract as every other reasoner, same four typed tools, same confidence
gates - only the transport differs. The loop is the real thing: the model asks
for a tool, we execute it locally, we feed the result back, and we repeat until
it answers or we hit ``ARGUS_AGENT_MAX_STEPS``.

Two properties matter more than anything else here:

* the model never sees pixels, only bounded numeric facts from the tools;
* if Bedrock is unreachable, throttled, or answers with something unparseable,
  the decision still gets made - by :class:`~argus.agent.HeuristicReasoner` -
  and the reason is visible in the decision trace as ``bedrock_fallback``.
"""

from __future__ import annotations

import os
from typing import Any

from .agent import (
    HeuristicReasoner,
    ToolRequest,
    ToolTrace,
    fallback,
    run_tool_loop,
    scored_from_model,
    tool_config_bedrock,
)
from .config import PolicyConfig
from .policy import Evidence, PolicyDecision

DEFAULT_MODEL = "anthropic.claude-3-5-sonnet-20241022-v2:0"


def bedrock_model() -> str:
    """``ARGUS_BEDROCK_MODEL``, or the ``*_MODEL_ID`` name used by infra/terraform."""

    return (
        os.environ.get("ARGUS_BEDROCK_MODEL", "")
        or os.environ.get("ARGUS_BEDROCK_MODEL_ID", "")
        or DEFAULT_MODEL
    )


def bedrock_region() -> str:
    return (
        os.environ.get("ARGUS_BEDROCK_REGION", "")
        or os.environ.get("AWS_REGION", "")
        or os.environ.get("AWS_DEFAULT_REGION", "")
    )


def bedrock_available() -> bool:
    """True when the SDK is importable, a region is known and a sink is on."""

    from .aws_store import boto3_available

    return boto3_available() and bool(bedrock_region())


PROMPT = """You are a video-triage reasoner for a security camera system.
You have tools that report bounded numeric facts about one evidence bundle.
Call the tools you need - clip_summary, motion_profile, timeline, gate_check -
then judge the evidence against these gates: motion_present, salient_subject,
illumination_usable, temporal_persistence.

Rules:
- Never invent observations that are not in the tool output.
- Return JSON only: {"confidence": <0..1>, "rationale": "<one sentence>"}.
- If the evidence is ambiguous, return a middling confidence; the system
  escalates to a human precisely when you are unsure."""


class BedrockReasoner:
    """Tool-calling reasoner backed by the Bedrock Converse API."""

    name = "bedrock-tools-v1"

    def __init__(
        self,
        cfg: PolicyConfig,
        model: str | None = None,
        region: str | None = None,
        client: Any | None = None,
    ) -> None:
        self.cfg = cfg
        self.model = model or bedrock_model()
        self.region = region if region is not None else bedrock_region()
        self._client = client
        self.calls = 0

    # ------------------------------------------------------------------ setup

    @property
    def client(self) -> Any:
        """Lazily built ``bedrock-runtime`` client; injectable for tests."""

        if self._client is None:
            import boto3

            kwargs = {"region_name": self.region} if self.region else {}
            self._client = boto3.client("bedrock-runtime", **kwargs)
        return self._client

    def converse(self, messages: list[Any], tool_config: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        return self.client.converse(
            modelId=self.model,
            messages=messages,
            toolConfig=tool_config,
            inferenceConfig={"temperature": 0.0, "maxTokens": 512},
        )

    # --------------------------------------------------------------- the loop

    def reason(self, evidence: Evidence, trace: ToolTrace) -> PolicyDecision:
        backend = _BedrockToolLoop(self, trace)
        try:
            text = run_tool_loop(evidence, trace, backend, initial=backend.initial())
            return scored_from_model(evidence, self.cfg, text)
        except Exception as exc:  # noqa: BLE001 — a decision is always produced
            return fallback(evidence, trace, self.cfg, "bedrock", exc)


class _BedrockToolLoop:
    """Adapts the Converse API to :func:`~argus.agent.run_tool_loop`."""

    def __init__(self, owner: BedrockReasoner, trace: ToolTrace) -> None:
        self.owner = owner
        self.trace = trace
        self.tool_config = tool_config_bedrock()

    def initial(self) -> list[dict[str, Any]]:
        return [{"role": "user", "content": [{"text": PROMPT}]}]

    def respond(self, messages: list[Any], step: int) -> tuple[Any, list[ToolRequest], str]:
        with self.trace.call("bedrock_step", step=step, model=self.owner.model):
            response = self.owner.converse(messages, self.tool_config)

        message = (response.get("output") or {}).get("message") or {}
        blocks = message.get("content") or []
        requests: list[ToolRequest] = []
        for i, block in enumerate(blocks):
            use = block.get("toolUse")
            if not use:
                continue
            requests.append(
                ToolRequest(
                    id=str(use.get("toolUseId") or f"tool_{step}_{i}"),
                    name=str(use.get("name") or ""),
                    arguments=use.get("input") or {},
                )
            )
        text = "\n".join(str(b["text"]) for b in blocks if "text" in b)
        assistant: dict[str, Any] | None = None
        if blocks:
            assistant = {"role": "assistant", "content": blocks}
        return assistant, requests, text

    def tool_result(
        self, requests: list[ToolRequest], results: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        blocks = [
            {
                "toolResult": {
                    "toolUseId": r.id,
                    "content": [{"json": res}],
                    "status": "error" if "error" in res else "success",
                }
            }
            for r, res in zip(requests, results, strict=False)
        ]
        return [{"role": "user", "content": blocks}]


__all__ = [
    "BedrockReasoner",
    "HeuristicReasoner",
    "DEFAULT_MODEL",
    "bedrock_available",
    "bedrock_model",
    "bedrock_region",
]
