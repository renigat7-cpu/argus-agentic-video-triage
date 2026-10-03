# Argus — Agentic Video Triage (OpenCV 5 + AWS)

[![CI](https://github.com/renigat7-cpu/argus-agentic-video-triage/actions/workflows/ci.yml/badge.svg)](https://github.com/renigat7-cpu/argus-agentic-video-triage/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Argus watches long video and answers one question: **did something happen that a
human should look at?** It uses **OpenCV 5** to cheaply rank frames by how much is
happening, then spends an expensive, tool-using **agent** only on the top candidates.
Every decision is `confirm`, `dismiss`, or `escalate` — and low confidence is always
escalated to a human, never guessed.

> Built for the **OpenCV AI Competition 2026, powered by AWS** and entered for the
> **Agentic Vision Award**.

## Why

A single 8 MP camera produces more footage than any team can review. The bottleneck
is not storage or detection — it is the reasoning-bound step of deciding *which*
frames deserve scrutiny. Argus moves that decision to the cheapest layer that can
be trusted (OpenCV 5 signal processing) and reserves expensive reasoning for the
frames that survive.

## How it works

```
        ┌────────────┐   signals   ┌───────────────┐  top-K  ┌──────────────────┐
 clip → │  OpenCV 5  │ ──────────► │  selection +  │ ──────► │  agent (tools)   │
         │  ingest    │  motion,    │  temporal +   │ frames │  gates → decision │
         │  + decode  │  edges,     │  pHash        │ (facts)│  confirm/dismiss/ │
         └────────────┘  saliency   │  diversity    │         │  escalate         │
              │            colour   └───────────────┘         └────────┬─────────┘
              ▼                                                          ▼
        CloudWatch / S3 ◄───────────── decision log + tool trace ──── human (escalate)
```

1. **Frame selection (OpenCV 5).** For each sampled frame Argus computes inter-frame
   motion (difference + Farneback dense optical flow), Canny edge density, saliency
   from OpenCV contrib, an HSV histogram colour-anomaly score, and an OpenCV-5
   perceptual hash (`cv2.img_hash.PHash`). These are combined into a weighted score
   with a **temporal-diversity constraint**, so a burst of near-identical frames
   cannot consume the budget — the perceptual hash rejects visually redundant
   candidates even when they are far apart in time.
2. **Agentic verification.** A tool-using agent calls four typed tools —
   `clip_summary`, `motion_profile`, `timeline`, `gate_check` — and evaluates four
   gates: `motion_present`, `salient_subject`, `illumination_usable`,
   `temporal_persistence`. **There is no frame-fetching tool**: the agent only ever
   receives bounded numeric facts (peak motion, saliency, luma, a short signal
   timeline), never raw pixels, which is what keeps its cost and input size
   predictable. The gates combine into a confidence that yields `confirm`, `dismiss`,
   or `escalate`.
3. **Human control.** Only `escalate` reaches a human, together with the evidence
   bundle (frames, gate results, rationale, tool trace). The reviewer records a
   verdict through `POST /api/verdict`; the queue is browsable at `GET /api/escalations`
   and in the UI review panel.
4. **Observability.** Every decision ships with a JSONL record and per-tool trace, so
   any result can be reconstructed. The judge-visible copy lives in
   [`docs/evidence/decisions.jsonl`](docs/evidence/decisions.jsonl) — the runtime
   log in `var/` is gitignored, so the evidence directory is the copy that ships.

The OpenCV 5 output is not decoration: the numeric signals decide *which frames the
agent may reason about, which measurements it can read, and which gates can pass*.
Remove the OpenCV stage and the agent has nothing to act on.

## Demo video

▶️ **[youtube.com/watch?v=k9v5T1SAocA](https://www.youtube.com/watch?v=k9v5T1SAocA)** —
walkthrough of triage, the escalation queue and a human verdict.

**Disclosure:** the narration is synthetic (AI text-to-speech) and the on-screen
presenter is a generated graphic — no human presenter appears. The system shown is
the real code in this repository.

## Quickstart

### Docker (recommended)

```bash
git clone https://github.com/renigat7-cpu/argus-agentic-video-triage.git
cd argus-agentic-video-triage
cp .env.example .env      # optional: leave empty to run fully local
docker compose up --build
# UI + API on http://localhost:8080
```

### Local

```bash
# The venv reuses system site-packages so a prebuilt OpenCV 5 wheel
# (opencv-contrib-python) does not have to be downloaded again.
python3 -m venv --system-site-packages .venv
. .venv/bin/activate
make install              # pip install -r requirements.txt && pip install -e ".[dev]"
make test                 # unit tests
make eval                 # evaluation harness -> var/eval/metrics.json
make api                  # demo UI on http://localhost:8080
```

`make dev` installs only the dev extras (`pytest`, `ruff`, `mypy`) and is what
`make lint` expects. Pinned dependencies: `opencv-contrib-python==5.0.0.93` plus
`boto3` for the AWS features. See `requirements.txt`; `make lock` regenerates the
curated exact-pin `requirements.lock`.

## API

| Method | Path                 | Purpose                                                        |
| ------ | -------------------- | -------------------------------------------------------------- |
| GET    | `/healthz`           | liveness + OpenCV/reasoner versions                             |
| GET    | `/`                  | demo UI (triage + review panel)                                 |
| POST   | `/api/triage`        | triage one uploaded clip                                        |
| POST   | `/api/triage-batch`  | triage several clips                                            |
| GET    | `/api/decisions`     | recent decision records                                         |
| GET    | `/api/escalations`   | open human-in-the-loop escalation queue                         |
| POST   | `/api/verdict`       | record a human verdict on an escalated decision (JSON body)      |
| GET    | `/api/capabilities`  | what this deployment can actually do (OpenCV/reasoner/AWS state) |

```bash
curl -f -X POST http://localhost:8080/api/triage \
     -F "file=@clip.mp4" -F "location=cam-1"

curl -s http://localhost:8080/api/escalations | jq
```

## Human-in-the-loop

Low confidence is never resolved by guessing. When the agent lands in the escalation
band it is queued with its evidence bundle and waits for a person:

- `GET /api/escalations` — the open queue (decision, confidence, rationale, the
  evidence frames and the tool trace that produced it). The UI review panel renders
  the same list.
- `POST /api/verdict` — the reviewer's answer: `{"run_id": ..., "verdict":
  "approve" | "reject" | "needs_more", "reviewer": ..., "note": ...}`. Verdicts are
  appended to `verdicts.jsonl` and supersede the open escalation, so human decisions
  are auditable and usable to calibrate the confirm/dismiss thresholds.
- The policy keeps a human out of the loop for the easy cases: `dismiss_at <=
  confidence < confirm_at` is exactly the band that escalates, and a failed hard gate
  (`motion_present`) can never be auto-confirmed.

## Failure handling

Failures are converted into decisions with an explicit rationale, not into HTTP
errors or silent passes:

| Failure | Behaviour |
| --- | --- |
| Undecodable / empty / truncated clip | `decision = "escalate"` with rationale `decode_error: ...` — a broken camera feed is a thing for a human to look at, not a 400 |
| Reasoner outage (Bedrock/OpenAI error, timeout, throttling, unparsable output) | fall back to the deterministic heuristic reasoner; the decision is still produced and the fallback is recorded in the trace |
| Bedrock tool loop does not converge | stopped at the step budget (`ARGUS_AGENT_MAX_STEPS`, default 6) and the partial trace is kept |
| S3/CloudWatch sink unavailable | the decision is already written to the local log; the AWS write is retried and never blocks the decision |
| Dark / blown-out frames | `illumination_usable` gate fails → confidence is capped, so it cannot auto-confirm |
| Ambiguous evidence | `escalate` to a human, never guess |

## Evaluation

`make eval` builds a synthetic dataset with a known ground-truth event window,
runs the full pipeline, and writes `var/eval/metrics.json` including:

- precision / recall / F1 and **missed-event rate**
- **selection recall** (did frame reduction keep the event?)
- **cost per stream-hour** and latency

The committed, judge-visible copy of every artefact is in
[`docs/evidence/`](docs/evidence/README.md) (`make evidence` refreshes it).
Failure cases and limitations are discussed in `docs/REPORT.md`.

## AWS deployment

Everything below is provisioned by `infra/terraform` — see
[`infra/terraform/README.md`](infra/terraform/README.md).

- **Amazon Bedrock** — the `bedrock` reasoner backend. Argus runs a real tool-use
  loop over the Bedrock **Converse API** (`bedrock-runtime:Converse`): the model is
  offered the JSON schemas of `clip_summary`, `motion_profile`, `timeline` and
  `gate_check`, emits `toolUse` blocks, Argus executes them, feeds the `toolResult`
  blocks back, and repeats until the model returns a final answer or the step budget
  (`ARGUS_AGENT_MAX_STEPS`) is spent. The `llm` backend does the same loop over an
  OpenAI-compatible `tool_calls` API. The task role is granted
  `bedrock:InvokeModel` / `bedrock:InvokeModelWithResponseStream` on the configured
  model ARNs.
- **S3 decision log** — set `ARGUS_AWS_SINK=s3` and the `AwsSink`
  (`src/argus/aws_store.py`) writes every decision record as JSON to
  `s3://$ARGUS_S3_BUCKET/$ARGUS_S3_PREFIX` (prefix `decisions/`) keeping the same
  schema as the local log.
- **CloudWatch metrics** — the same sink publishes decision counts, latency and
  cost under `ARGUS_CW_NAMESPACE` (`PutMetricData`), so an operator gets a dashboard
  without scraping JSONL.
- **Lambda (S3-triggered)** — a separate **ingress** bucket carries an
  `s3:ObjectCreated:*` notification wired to `aws_lambda_function`, which runs
  `src/argus/lambda_handler.py` (`argus.lambda_handler:handler`) and writes its
  decision into the evidence bucket. No API call and no always-on cost, and because
  results never land in the ingress bucket the trigger cannot loop.
- **ECS Fargate** — the FastAPI service (`/healthz`, `/api/triage`, `/api/decisions`,
  `/api/escalations`, `/api/verdict`) with a container health check and an EFS mount
  for `ARGUS_OUT_DIR`, so the decision log survives task replacement.
- **ECR** — immutable tags with scan-on-push, so a published tag is a permanent,
  addressable statement about exactly which bytes shipped.

Relevant environment variables:

| Variable | Purpose |
| --- | --- |
| `ARGUS_REASONER` | `heuristic` (default, deterministic) or `bedrock` |
| `ARGUS_AGENT_MAX_STEPS` | tool-use loop step budget (default 6) |
| `ARGUS_BEDROCK_MODEL_ID` | Bedrock model id used by the `bedrock` reasoner |
| `ARGUS_AWS_SINK` | `s3` to enable the AWS sink (unset = local only) |
| `ARGUS_AWS_REGION`, `ARGUS_S3_BUCKET`, `ARGUS_S3_PREFIX` | S3 destination |
| `ARGUS_CW_NAMESPACE` | CloudWatch metric namespace |

## Repository layout

```
src/argus/   pipeline: config, signals, selection, policy, agent, bedrock,
             aws_store, human, ingest, observability, pipeline, api, eval,
             cli, lambda_handler
web/         FastAPI demo UI (triage + human review panel)
tests/       unit tests + pipeline smoke test
docs/        architecture diagram, technical report, judge-visible evidence
infra/       Terraform for AWS (S3, ECR, ECS, EFS, Lambda, CloudWatch, IAM)
```

## Responsible use

Argus is a triage aid, not an autonomous authority. It deliberately escalates under
uncertainty, keeps a human in the loop for consequential decisions, logs everything
it does, and performs no face recognition or identity inference. Deployments should
respect local privacy law and obtain any required consent for camera use.

## License

MIT.
