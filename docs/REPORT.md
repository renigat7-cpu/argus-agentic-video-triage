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
   signals and ranked; only the top-K temporally diverse candidates survive.
2. **Bounded agentic reasoning.** A tool-using agent evaluates the candidates against
   an explicit policy and returns `confirm`, `dismiss`, or `escalate`.

The split is the safety argument: OpenCV is deterministic and cannot hallucinate,
while the agent is flexible but only ever sees frames OpenCV chose and numbers OpenCV
measured. See `docs/architecture.md` for the component and sequence diagrams.

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

**Frame selection** (`src/argus/selection.py`) combines these into a weighted score
and then enforces a **temporal-diversity constraint** (minimum spacing) so a burst of
near-identical high-motion frames cannot consume the whole budget. Only the top-K
diverse frames are passed to the agent.

Because the signals drive selection, the OpenCV output changes the agent's behaviour:
the frames the agent can fetch, the measurement tools it calls, and the gates that can
pass all depend on the numeric output of the OpenCV stage.

## 4. Agent and policy

The agent (`src/argus/agent.py`) calls typed tools only — clip metadata, per-frame
signal timeline, and a gate check — and never sees raw video. Two interchangeable
backends share one contract:

- `HeuristicReasoner` (default): deterministic, no network, reproducible in CI.
- `LLMReasoner`: an OpenAI-compatible tool-calling backend, used when configured.

Four explicit gates (`src/argus/policy.py`) are evaluated: `motion_present`,
`salient_subject`, `illumination_usable`, `temporal_persistence`. A failed gate caps
confidence below the confirm threshold, so a hard gate can never be auto-confirmed. A
missing-motion gate can never confirm at all. The resulting confidence routes to
`confirm`, `dismiss`, or the **escalation band**, which hands the evidence to a human.

## 5. AWS deployment

`infra/terraform` provisions:

- **S3** — clip ingress and decision/evidence bundles (private, auto-expiring).
- **ECS Fargate** — the FastAPI service (`/healthz`, `/api/triage`, `/api/triage-batch`,
  `/api/decisions`), with a container health check.
- **ECR** — immutable-addressable image storage.
- **CloudWatch** — container logs and Container Insights metrics.

The image is built from `Dockerfile` (pinned `opencv-contrib-python==5.0.0.93`, ffmpeg,
non-root user, read-only root in compose). Reproducibility is explicit: exact pins in
`requirements.lock`, one `docker compose up --build` path, and a `make` target for each
stage.

## 6. Evaluation

### 6.1 Method

`python -m argus.eval --clips 12` generates a labelled synthetic dataset (half the
clips contain a brief, small, fast-moving event), runs the full pipeline, and writes
`var/eval/metrics.json`. It reports the metric that matters operationally —
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
| Mean latency | **6.6 s / 20 s clip** | — |

Frames scanned 1008 → frames selected 144 (reduction 0.857). At the current sampling
rate this is an ~7x reduction in the work sent to reasoning.

### 6.3 Interpretation and honesty

On this synthetic set the motion-only baseline already separates the clips, so Argus
does **not** beat it on classification here. Argus's measured advantage on this
dataset is **cost** (85.7% fewer frames evaluated) and **safety envelope** (every
negative is escalated with evidence rather than silently dismissed, and a failed gate
cannot confirm). The synthetic benchmark is a reproducibility smoke test, not a claim
of real-world performance; it is deliberately not tuned to flatter the system.

### 6.4 Failure cases and limitations

- **Latency.** Mean 6.6 s per 20 s clip is dominated by dense Farneback flow per frame.
  The heuristic reasoner is free; the bottleneck is perception, which is exactly where
  a deployment would move to GPU or a coarser sampling schedule.
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
  frames the agent may fetch, the tool calls it makes, and the gate outcomes; the tool
  trace and decision record make this explicit and inspectable in `var/decisions.jsonl`.
- **Task success / failure handling / observability / human control** — Section 6
  reports success and failure metrics; every decision is logged with rule results and a
  tool trace; ambiguity is escalated to a human rather than guessed.

## 8. Responsible use

Argus is a triage aid, not an autonomous authority. It deliberately escalates under
uncertainty, keeps a human in the loop for consequential decisions, logs everything it
does, and performs no identity inference. Deployments must respect local privacy law
and obtain any required consent for camera use.
