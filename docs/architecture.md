# Argus Architecture

## Component view

```mermaid
flowchart LR
  subgraph Ingest
    V[Video source]
    D[OpenCV 5 decode + sample]
  end

  subgraph Selection["Frame selection (OpenCV 5)"]
    M[Motion: diff + Farneback flow]
    E[Edges: Canny density]
    S[Saliency: OpenCV contrib]
    C[Colour anomaly: HSV histogram]
    R[Weighted rank + temporal diversity]
  end

  subgraph Agent["Verification agent (tools)"]
    T1[get_clip_meta]
    T2[get_frame]
    T3[measure_signals]
    T4[read_policy]
    G[Gates: motion, saliency, illumination, persistence]
    DEC{confirm / dismiss / escalate}
  end

  subgraph AWS
    S3[(S3 clips + evidence)]
    L[Lambda triage]
    ECS[ECS Fargate API]
    CW[CloudWatch metrics + logs]
  end

  V --> D --> M & E & S & C --> R --> T1
  R -->|top-K frames| T2 --> T3 --> G --> DEC
  T4 --> G
  DEC -->|escalate| H[Human reviewer]
  DEC --> S3
  DEC --> CW
  ECS --> R
  ECS --> Agent
  S3 --> L --> Agent
```

## Agent workflow (perception → decision → action)

```mermaid
sequenceDiagram
  participant P as OpenCV 5 (perception)
  participant A as Agent (orchestration)
  participant T as Tools
  participant H as Human

  P->>A: ranked frames + signal scores
  A->>T: get_clip_meta(clip)
  T-->>A: fps, duration, resolution
  loop top-K candidates
    A->>T: measure_signals(frame_id)
    T-->>A: motion, edge, saliency, colour
    A->>A: evaluate gates vs policy
  end
  alt confidence >= confirm_at and motion present
    A->>T: record decision=confirm
  else confidence <= dismiss_at
    A->>T: record decision=dismiss
  else ambiguous band
    A->>H: escalate with evidence bundle
    H-->>A: human verdict (logged, used for calibration)
  end
```

## Why the split matters

The OpenCV 5 stage is the **safety floor**: it is deterministic, cheap, and cannot
hallucinate. The agent is the **flexible layer**: it can reason about policy and
edge cases, but it only ever sees the frames OpenCV chose and the signals OpenCV
measured. This keeps cost bounded (`n_frames ≫ selected_frames`) and keeps every
agent action traceable back to a numeric signal.

## Data flow and observability

- `var/uploads/` — staged uploads
- `var/decisions.jsonl` — one record per triage: decision, confidence, rule results,
  selected frames, cost, tool trace
- `var/runs.jsonl` — one record per batch/eval run
- AWS: same records to S3 (evidence) and CloudWatch (metrics)

## Failure handling

| Failure                   | Behaviour                                             |
| ------------------------- | ----------------------------------------------------- |
| Undecodable / empty clip  | `dismiss` with `decode_error` note, logged            |
| Dark / blown-out frames   | `illumination_usable` fails → confidence capped       |
| No motion at all          | cannot auto-confirm (hard rule) → `escalate`          |
| LLM backend unavailable   | fall back to deterministic heuristic reasoner         |
| Ambiguous evidence        | `escalate` to human, never guess                      |
