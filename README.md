# Argus — Agentic Video Triage (OpenCV 5 + AWS)

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
        │  ingest    │  motion,    │  diversity    │  frames │  gates → decision │
        │  + decode  │  edges,     │  constraint   │         │  confirm/dismiss/ │
        └────────────┘  saliency   └───────────────┘         │  escalate         │
              │            colour                                └────────┬─────────┘
              ▼                                                          ▼
        CloudWatch / S3 ◄───────────── decision log + tool trace ──── human (escalate)
```

1. **Frame selection (OpenCV 5).** For each sampled frame Argus computes inter-frame
   motion (difference + Farneback dense optical flow), Canny edge density, saliency
   from OpenCV contrib, and an HSV histogram colour-anomaly score. These are combined
   into a weighted score with a **temporal-diversity constraint** so a burst of
   near-identical frames cannot consume the budget.
2. **Agentic verification.** A tool-using agent calls typed tools (clip metadata,
   fetch frame, measure motion/edges/saliency, read policy) and evaluates four gates:
   `motion_present`, `salient_subject`, `illumination_usable`, `temporal_persistence`.
   The gates combine into a confidence that yields `confirm`, `dismiss`, or `escalate`.
3. **Human control.** Only `escalate` reaches a human, together with the evidence
   bundle (frames, gate results, rationale, tool trace).
4. **Observability.** Every decision ships with a JSONL record and per-tool trace, so
   any result can be reconstructed. See `var/decisions.jsonl`.

The OpenCV 5 output is not decoration: the numeric signals decide *which frames the
agent fetches, which measurement tools it calls, and which gates can pass*. Remove
the OpenCV stage and the agent has nothing to act on.

## Quickstart

### Docker (recommended)

```bash
git clone <REPOSITORY_URL> && cd argus
docker compose up --build
# UI + API on http://localhost:8080
```

### Local

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt && pip install -e ".[dev]"
make test        # unit tests
make eval        # evaluation harness -> var/eval/metrics.json
make api         # demo UI on http://localhost:8080
```

Pinned dependencies: `opencv-contrib-python==5.0.0.93`. See `requirements.txt`.

## API

| Method | Path                 | Purpose                                            |
| ------ | -------------------- | -------------------------------------------------- |
| GET    | `/healthz`           | liveness + OpenCV/reasoner versions                |
| GET    | `/`                  | demo UI                                            |
| POST   | `/api/triage`        | triage one uploaded clip                           |
| POST   | `/api/triage-batch`  | triage several clips                               |
| GET    | `/api/decisions`     | recent decision records                            |

```bash
curl -f -X POST http://localhost:8080/api/triage \
     -F "file=@clip.mp4" -F "location=cam-1"
```

## Evaluation

`make eval` builds a synthetic dataset with a known ground-truth event window,
runs the full pipeline, and writes `var/eval/metrics.json` including:

- precision / recall / F1 and **missed-event rate**
- **selection recall** (did frame reduction keep the event?)
- **cost per stream-hour** and latency

Failure cases and limitations are discussed in `docs/REPORT.md`.

## AWS deployment

`infra/terraform` provisions S3 (clips + evidence), ECS Fargate (API), Lambda
(event-driven triage) and CloudWatch (metrics/logs). See `infra/terraform/README.md`.

## Repository layout

```
src/argus/   pipeline: config, signals, selection, policy, agent, ingest,
             observability, pipeline, api, eval, cli
web/         FastAPI demo UI
tests/       unit tests + pipeline smoke test
docs/        architecture diagram and technical report
infra/       Terraform for AWS
```

## Responsible use

Argus is a triage aid, not an autonomous authority. It deliberately escalates under
uncertainty, keeps a human in the loop for consequential decisions, logs everything
it does, and performs no face recognition or identity inference. Deployments should
respect local privacy law and obtain any required consent for camera use.

## License

MIT.
