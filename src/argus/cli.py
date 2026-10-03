"""Command line interface.

Subcommands mirror the pipeline stages so each can be exercised and audited
independently, which is what makes the system testable end to end.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .config import load_config
from .ingest import extract_signals, probe
from .pipeline import TriagePipeline, clip_meta_dict, summarize
from .selection import select_frames


def _with_out(cfg, out: str | None):
    if not out:
        return cfg
    return type(cfg)(
        selection=cfg.selection, policy=cfg.policy, cost=cfg.cost,
        out_dir=Path(out), decision_log=cfg.decision_log, run_log=cfg.run_log,
    )


def _build(args: argparse.Namespace) -> TriagePipeline:
    cfg = _with_out(load_config(args.config), getattr(args, "out", None))
    return TriagePipeline(cfg, reasoner_backend=args.reasoner)


def cmd_probe(args: argparse.Namespace) -> int:
    print(json.dumps(clip_meta_dict(probe(args.clip)), indent=2))
    return 0


def cmd_select(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    signals, meta = extract_signals(args.clip, cfg.selection)
    candidates, diag = select_frames(signals, cfg.selection)
    chosen = [c for c in candidates if c.selected]
    print(json.dumps({"meta": clip_meta_dict(meta), "diagnostics": diag,
                      "selected": [{"t": c.signals.timestamp_s, "score": c.score}
                                   for c in chosen]}, indent=2))
    return 0


def cmd_triage(args: argparse.Namespace) -> int:
    pipeline = _build(args)
    result = pipeline.triage(args.clip, location=args.location)
    print(json.dumps(result.to_dict(), indent=2))
    return 0


def cmd_batch(args: argparse.Namespace) -> int:
    pipeline = _build(args)
    results = pipeline.triage_many(args.clips, location=args.location)
    print(json.dumps({"summary": summarize(results),
                      "results": [r.to_dict() for r in results]}, indent=2))
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    from . import eval as eval_mod

    out = Path(args.out or "var/eval")
    cfg = load_config(args.config)
    if args.out:
        cfg = type(cfg)(
            selection=cfg.selection, policy=cfg.policy, cost=cfg.cost,
            out_dir=out / "runs", decision_log=cfg.decision_log, run_log=cfg.run_log,
        )
    pipeline = TriagePipeline(cfg, reasoner_backend=args.reasoner)
    truths = eval_mod.build_dataset(out / "dataset", n_clips=args.clips)
    metrics, rows, _ = eval_mod.evaluate(pipeline, truths)
    out.mkdir(parents=True, exist_ok=True)
    (out / "metrics.json").write_text(json.dumps(metrics.to_dict(), indent=2), encoding="utf-8")
    (out / "rows.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
    print(json.dumps(metrics.to_dict(), indent=2))
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run("argus.api:app", host=args.host, port=args.port)
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="argus", description="Agentic video triage")
    ap.add_argument("--config", default=None, help="JSON config override")
    ap.add_argument("--reasoner", default="heuristic", choices=["heuristic", "llm", "bedrock"])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("probe", help="print clip metadata")
    p.add_argument("clip")
    p.set_defaults(func=cmd_probe)

    p = sub.add_parser("select", help="show frame selection only")
    p.add_argument("clip")
    p.set_defaults(func=cmd_select)

    p = sub.add_parser("triage", help="triage one clip")
    p.add_argument("clip")
    p.add_argument("--location", default="unknown")
    p.set_defaults(func=cmd_triage)

    p = sub.add_parser("batch", help="triage several clips")
    p.add_argument("clips", nargs="+")
    p.add_argument("--location", default="unknown")
    p.set_defaults(func=cmd_batch)

    p = sub.add_parser("eval", help="run the evaluation harness")
    p.add_argument("--clips", type=int, default=12)
    p.add_argument("--out", default="var/eval")
    p.set_defaults(func=cmd_eval)

    p = sub.add_parser("serve", help="run the web endpoint")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8080)
    p.set_defaults(func=cmd_serve)
    return ap


def main() -> int:
    args = build_parser().parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())