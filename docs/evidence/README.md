# Evaluation evidence

This directory is the committed, judge-accessible copy of the artefacts produced by
`python -m argus.eval --clips 12 --out var/eval` (or `make evidence`). Everything here is
reproducible from the repository with no private data: the harness generates its own
labelled synthetic clips.

## Files

| File | Meaning |
| --- | --- |
| `metrics.json` | Clip-level metrics plus cost, latency and the motion-only baseline. |
| `rows.json` | Per-clip ground truth and decision (the raw table behind the metrics). |
| `summary.json` | Batch summary for the 12-clip run. |
| `decisions.jsonl` | One full decision record per clip, including **OpenCV rule values**, **tool calls**, and the final action. |
| `live_api_decisions.jsonl` | Two records captured from the running FastAPI endpoint (`POST /api/triage`), one positive and one negative clip. |

## Reproduce

```bash
make evidence          # runs the harness and refreshes this directory
```

## OpenCV 5 output drives the decision (Agentic Vision evidence)

Each record in `decisions.jsonl` carries the numeric OpenCV signals that the selector and
the policy gates consumed. The 12-clip run resolves cleanly, and the *reason* is visible
in the numbers rather than asserted:

| clip | event | decision | conf | peak motion | peak saliency | frames |
| --- | --- | --- | ---: | ---: | ---: | --- |
| clip_000 | yes | confirm | 1.00 | 0.569 | 0.689 | 84 → 3 |
| clip_001 | no | escalate | 0.79 | 0.141 | 0.716 | 84 → 21 |
| clip_002 | yes | confirm | 1.00 | 0.574 | 0.715 | 84 → 3 |
| clip_003 | no | escalate | 0.79 | 0.169 | 0.721 | 84 → 21 |
| clip_004 | yes | confirm | 1.00 | 0.546 | 0.808 | 84 → 3 |
| clip_005 | no | escalate | 0.79 | 0.158 | 0.745 | 84 → 21 |
| clip_006 | yes | confirm | 1.00 | 0.556 | 0.725 | 84 → 3 |
| clip_007 | no | escalate | 0.79 | 0.145 | 0.538 | 84 → 21 |
| clip_008 | yes | confirm | 1.00 | 0.574 | 0.526 | 84 → 3 |
| clip_009 | no | escalate | 0.79 | 0.149 | 0.655 | 84 → 21 |
| clip_010 | yes | confirm | 1.00 | 0.557 | 0.571 | 84 → 3 |
| clip_011 | no | escalate | 0.79 | 0.154 | 0.757 | 84 → 21 |

The mechanism, end to end:

1. **OpenCV measures.** `cv2.calcOpticalFlowFarneback` + `cv2.absdiff` produce the motion
   value; `cv2.saliency.StaticSaliencySpectralResidual` (contrib) produces the saliency
   value. Positive clips measure motion ≈ 0.55; negative clips ≈ 0.14–0.17 (motion
   threshold 0.18).
2. **Selection reacts.** The weighted score, under the temporal-diversity constraint,
   selects only 3 frames for a positive burst (all near the event) versus 21 spread
   frames for a negative clip — the OpenCV output changes *what the agent is allowed to
   see*.
3. **The gate reacts.** `motion_present` passes only when the measured motion clears
   0.18. On negatives it fails, which **caps confidence at 0.79 and forces `escalate`**
   instead of `confirm`. A failed hard gate can never be auto-confirmed.
4. **The agent reacts.** The `tool_calls` in each record (`clip_summary`,
   `motion_profile`, `timeline`, `gate_check`) are the typed tools the agent invokes
   against the selected frames; the outcome feeds the final `confirm` / `escalate`.

This is the requested trace showing that OpenCV 5 output changes a later decision, tool
call, or action — not a slide, but the logged numbers themselves.
