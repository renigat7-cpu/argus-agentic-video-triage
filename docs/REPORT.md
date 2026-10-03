# Argus — Technical Report

*OpenCV AI Competition 2026, powered by AWS — Agentic Vision Award entry.*

## 1. Problem and users

Security, retail-loss and compliance teams are asked to watch more camera footage
than any human can. A single camera at 25 fps produces ~2.2 million frames per day.
The bottleneck is not storage or detection: it is the expensive, reasoning-bound
step of deciding **which** frames deserve scrutiny. Systems that run a heavy model
on every frame are unaffordable; systems that run only a naive motion trigger miss
slow-onset events and give no way to audit a missed alert.

Users are:

- **Operators** who need a short list of candidate events with evidence, not a wall
  of raw video.
- **Reviewers** who must be able to reconstruct *why* the system made a decision.
- **Engineers** who need a system whose cost, latency and failure modes are
  measurable and reproducible.

## 2. System overview

Argus is a two-stage triage system:

1. **Cheap perception (OpenCV 5).** Every sampled frame is described by inexpensive
   signals and ranked; only the top-K temporally *and* perceptually diverse candidates
   survive.
2. **Bounded agentic reasoning.** A tool-using agent evaluates the candidates against
   an explicit policy and returns `confirm`, `dismiss`, or `escalate`.

The split is the safety argument: OpenCV is deterministic and cannot hallucinate,
while the agent is flexible but only ever reasons over the numeric facts OpenCV
measured for the frames OpenCV chose — it is never handed raw pixels. See
`docs/architecture.md` for the component and sequence diagrams.

## 3. OpenCV 5 implementation

All perception is OpenCV 5 (`opencv-contrib-python==5.0.0.93`), computed on a
downscaled grey/HSV pair so per-frame cost stays flat as resolution grows.

| Signal | OpenCV API | Purpose |
| --- | --- | --- |
| Motion (fast) | `cv2.absdiff` + Gaussian blur | inter-frame change |
| Motion (dense) | `cv2.calcOpticalFlowFarneback` | rescues slow-onset events |
| Edges | `cv2.Canny` | structure / texture density |
| Saliency | `cv2.saliency.StaticSaliencySpectralResidual` (contrib) | stands-out regions |
| Colour anomaly | `cv2.calcHist` + `cv2.compareHist` (Bhattacharyya) | scene colour shifts |
| Perceptual hash | `cv2.img_hash.PHash` (OpenCV 5, contrib) | drops visually redundant frames |

**Frame selection** (`src/argus/selection.py`) combines these into a weighted score
and then enforces a **temporal-diversity constraint** (minimum spacing) so a burst of
near-identical high-motion frames cannot consume the whole budget. Because temporal
spacing alone cannot catch the same static subject re-scored minutes apart, each
candidate also carries an OpenCV-5 **perceptual hash** (`cv2.img_hash.PHash`, 64-bit
DCT hash): candidates whose Hamming distance to an already-selected frame is below
the diversity threshold are rejected as visually redundant even when they pass the
spacing test. Only the top-K diverse frames are passed to the agent.

Because the signals drive selection, the OpenCV output changes the agent's behaviour:
which frames the agent may reason about, which measurement tools it can read, and
which gates can pass all depend on the numeric output of the OpenCV stage.

## 4. Agent and policy

The agent (`src/argus/agent.py`) is a genuine **tool-use loop**, not a single prompt.
Argus publishes four typed tools to the model — `clip_summary`, `motion_profile`,
`timeline`, `gate_check` — and the model **chooses which to call, in what order, and
how many times**. Argus executes the requested tool, returns the result to the model,
and repeats until the model produces a final verdict or the step budget
(`ARGUS_AGENT_MAX_STEPS`, default 6) is exhausted; an unconverged loop still yields a
decision plus the partial trace. There is no frame-fetching tool and the agent never
sees raw video — every tool returns **bounded numeric facts** (peak motion, peak
optical-flow magnitude, peak saliency, mean luma, a short signal timeline), which is
what keeps the model's input small and its cost predictable.

Three interchangeable backends share one contract and one tool registry:

- `HeuristicReasoner` (default): deterministic, no network, reproducible in CI.
- `BedrockReasoner` (`src/argus/bedrock.py`, `ARGUS_REASONER=bedrock`): Amazon Bedrock
  **Converse API** — the tools are sent as `toolConfig` JSON schemas, the model replies
  with `toolUse` content blocks, Argus answers with `toolResult` blocks, and the loop
  continues to the model's final message.
- `LLMReasoner`: the same loop over an OpenAI-compatible endpoint using `tool_calls`
  and `tool` messages.

All three return the same `PolicyDecision`, so the escalation policy cannot drift
between backends. If a model backend fails (timeout, throttling, transport error,
unparsable output) the pipeline falls back to the deterministic heuristic reasoner and
records the fallback in the trace: a reasoner outage degrades reasoning quality, never
availability.

Four explicit gates (`src/argus/policy.py`) are evaluated: `motion_present`,
`salient_subject`, `illumination_usable`, `temporal_persistence`. A failed gate caps
confidence below the confirm threshold, so a hard gate can never be auto-confirmed. A
missing-motion gate can never confirm at all. The resulting confidence routes to
`confirm`, `dismiss`, or the **escalation band**, which hands the evidence to a human
through the escalation queue (`GET /api/escalations`, verdict recorded with
`POST /api/verdict`, rendered by the UI review panel).

## 5. AWS deployment

`infra/terraform` provisions:

- **S3** — two private, encrypted buckets, split so the Lambda trigger is loop-free.
  The **ingress** bucket carries the notification and is never written back to; the
  **evidence** bucket, which has no notification, holds `decisions/` (the decision log
  written by the AWS sink), `results/` (evidence bundles), `evidence/` (archived eval
  artefacts) and `lambda/` (the deployment package). Lifecycle rules expire everything
  except the Lambda package.
- **Amazon Bedrock** — the reasoner backend when `ARGUS_REASONER=bedrock`. The task
  role is granted `bedrock:InvokeModel` / `bedrock:InvokeModelWithResponseStream`
  on the configured model ARNs, which is what the Converse tool-use loop calls.
- **Lambda** — `src/argus/lambda_handler.py` (`argus.lambda_handler:handler`),
  triggered by `s3:ObjectCreated:*` on the ingress bucket, with
  `aws_lambda_permission` scoped to that one bucket. Event-driven triage costs nothing
  when no clips arrive.
- **ECR** — **immutable** tags (`image_tag_mutability = "IMMUTABLE"`) with scan-on-push.
  A published tag is therefore a permanent, addressable statement about exactly which
  bytes shipped, and a lifecycle rule prunes all but the newest ten images.
- **ECS Fargate** — the FastAPI service (`/healthz`, `/api/triage`, `/api/triage-batch`,
  `/api/decisions`, `/api/escalations`, `/api/verdict`), with a container health check,
  a read-only root filesystem, and an **EFS access point mounted at `/data`** so
  `ARGUS_OUT_DIR` (the decision log) survives task replacement.
- **CloudWatch** — container and Lambda log groups, Container Insights metrics, and the
  Argus metric namespace the AWS sink publishes triage counts/latency/cost into.

API ingress is **not** open to the internet: the security group allows the container
port only from `allowed_cidr` (default `10.0.0.0/8`), which an operator sets to their
own egress range or to a fronting ALB's range.

The image is built from `Dockerfile`: the base image is pinned **by digest**
(`python:3.12-slim-bookworm@sha256:54c85f3c…`, a multi-architecture OCI index, so the
build resolves to identical image bytes on every host), with
`opencv-contrib-python==5.0.0.93` pinned in `requirements.txt`, ffmpeg for robust
decode, a non-root user (uid 10001) and a read-only root filesystem in compose.
Reproducibility is explicit: exact pins in `requirements.lock`, one
`docker compose up --build` path, and a `make` target for each stage.

## 6. Evaluation

### 6.1 Method

`python -m argus.eval --clips 12` generates a labelled synthetic dataset (half the
clips contain a brief, small, fast-moving event), runs the full pipeline, and writes
`var/eval/metrics.json`. A committed, judge-accessible copy of every artefact lives in
`docs/evidence/` (regenerate with `make evidence`). It reports the metric that matters operationally —
**missed-event rate** — next to precision/recall, plus selection recall, frame
reduction, cost per stream-hour and latency.

A **motion-only baseline** (confirm whenever the motion gate passes, with no agent
aggregation, saliency, persistence, or escalation) is computed on the same clips.

### 6.2 Results (12 clips, 6 positive / 6 negative)

| Metric | Argus | Motion-only baseline |
| --- | ---: | ---: |
| Precision | **1.00** | 1.00 |
| Recall | **1.00** | 1.00 |
| F1 | **1.00** | 1.00 |
| Missed-event rate | **0.00** | 0.00 |
| Selection recall | **1.00** | — |
| Frame reduction | **85.7%** | 0% |
| Escalation rate | **50%** | 0% |
| Cost / stream-hour | **$1.85** | (full-rate) |
| Mean latency | **8.41 s / 20 s clip** | — |

Frames scanned 1008 → frames selected 144 (reduction 0.857). At the current sampling
rate this is an ~7x reduction in the work sent to reasoning. The mean latency is
8411.34 ms as recorded in `docs/evidence/metrics.json` (`mean_latency_ms`) and
`docs/evidence/summary.json`; the whole 12-clip run took 135.71 s of wall clock.

### 6.3 Interpretation and honesty

On this synthetic set the motion-only baseline already separates the clips, so Argus
does **not** beat it on classification here. Argus's measured advantage on this
dataset is **cost** (85.7% fewer frames evaluated) and **safety envelope** (every
negative is escalated with evidence rather than silently dismissed, and a failed gate
cannot confirm). The synthetic benchmark is a reproducibility smoke test, not a claim
of real-world performance; it is deliberately not tuned to flatter the system.

### 6.4 Failure cases and limitations

- **Latency.** Mean 8.41 s (8411.34 ms, `docs/evidence/metrics.json`) per 20 s clip is
  dominated by dense Farneback flow per frame. The heuristic reasoner is free; the
  bottleneck is perception, which is exactly where a deployment would move to GPU or a
  coarser sampling schedule. With the `bedrock` reasoner the tool-use loop adds a small
  number of model round-trips on top of that, bounded by `ARGUS_AGENT_MAX_STEPS`.
- **Saliency is static.** Spectral-residual saliency does not distinguish a static
  salient background object from a moving subject; it is a supporting signal, not the
  decider. (A caching bug that silently zeroed saliency was found *by* this harness —
  evidence the evaluation is doing its job.)
- **Synthetic data.** Backgrounds are low-contrast but free of weather, night-time IR,
  crowds and camera motion; real-world false-positive behaviour is unmeasured.
- **Escalation load.** With a 50% escalation rate on ambiguous clips, human capacity
  becomes the constraint; thresholds must be calibrated per deployment.
- **No identity inference.** Argus performs no face recognition or re-identification.

## 7. Agentic Vision Award evidence

- **Agent workflow diagram** — `docs/architecture.md` (perception → decision → action
  sequence diagram).
- **OpenCV output changes later actions** — the signal ranking determines the top-K
  diverse frames the agent may reason about, the tool calls it can make (`clip_summary`,
  `motion_profile`, `timeline`, `gate_check`), and the gate outcomes; the tool trace and
  decision record make this explicit and inspectable in
  `docs/evidence/decisions.jsonl` (see `docs/evidence/README.md`).
- **Task success / failure handling / observability / human control** — Section 6
  reports success and failure metrics; every decision is logged with rule results and a
  tool trace; ambiguity is escalated to a human rather than guessed, and the reviewer's
  verdict is recorded via `POST /api/verdict` on the escalation queue.
- **Demo video** — https://www.youtube.com/watch?v=k9v5T1SAocA (the on-camera
  presenter is an AI-generated avatar; the system shown is this code).

## 8. Responsible use

Argus is a triage aid, not an autonomous authority. It deliberately escalates under
uncertainty, keeps a human in the loop for consequential decisions, logs everything it
does, and performs no identity inference. Deployments must respect local privacy law
and obtain any required consent for camera use.
