"""Command-line entry point for local runs and editorial decisions."""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .evaluation import evaluate_bundles
from .pipeline import DEFAULT_KNOWLEDGE, DEFAULT_RULES, PROJECT_ROOT, run_brief
from .schemas import Brief


DEFAULT_EVAL_BRIEFS = PROJECT_ROOT / "data" / "eval_briefs.jsonl"
DEFAULT_EVAL_GOLD = PROJECT_ROOT / "data" / "eval_gold.json"


def load_briefs(path: Path) -> list[Brief]:
    if path.suffix == ".jsonl":
        records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    else:
        content = json.loads(path.read_text(encoding="utf-8"))
        records = content if isinstance(content, list) else [content]
    briefs = [Brief.model_validate(record) for record in records]
    ids = [brief.id for brief in briefs]
    if len(ids) != len(set(ids)):
        raise ValueError("Brief IDs must be unique")
    return briefs


async def _execute(args: argparse.Namespace) -> int:
    briefs = load_briefs(args.briefs)
    if args.command == "run":
        matches = [brief for brief in briefs if brief.id == args.id]
        if not matches:
            raise ValueError(f"Brief {args.id!r} was not found in {args.briefs}")
        briefs = matches
    outcomes = []
    for brief in briefs:
        result, run_dir = await run_brief(
            brief,
            mode=args.mode,
            knowledge_path=args.knowledge,
            rules_path=args.rules,
            output_root=args.output,
        )
        outcomes.append({"brief_id": brief.id, "status": result.status, "run_dir": str(run_dir)})
        print(f"{brief.id}: {result.status} -> {run_dir}")
    if args.command == "demo":
        counts = Counter(outcome["status"] for outcome in outcomes)
        print("Demo summary:", json.dumps(dict(counts), ensure_ascii=False))
        print("Offline mode checks workflow behavior only; it is not an LLM quality evaluation.")
    return 2 if any(outcome["status"] == "model_error" for outcome in outcomes) else 0


def _find_bundles(paths: list[Path]) -> list[Path]:
    """Accept bundle files, run directories, or directories of run directories."""
    found: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        if path.is_file():
            candidates = [path]
        elif (path / "bundle.json").is_file():
            candidates = [path / "bundle.json"]
        elif path.is_dir():
            candidates = sorted(path.glob("*/bundle.json"))
        else:
            raise FileNotFoundError(path)
        if not candidates:
            raise FileNotFoundError(f"No bundle.json found under {path}")
        for candidate in candidates:
            resolved = candidate.resolve()
            if resolved not in seen:
                found.append(candidate)
                seen.add(resolved)
    return found


def _evaluate(args: argparse.Namespace) -> int:
    report = evaluate_bundles(_find_bundles(args.paths), args.gold)
    serialized = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output is None:
        print(serialized)
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized + "\n", encoding="utf-8")
        print(f"Saved deterministic evaluation -> {args.output}")
    return 0


async def _eval_run(args: argparse.Namespace) -> int:
    briefs = load_briefs(args.briefs)
    gold = json.loads(args.gold.read_text(encoding="utf-8"))
    gold_ids = [case["brief_id"] for case in gold["cases"]]
    if len(gold_ids) != len(set(gold_ids)) or {brief.id for brief in briefs} != set(gold_ids):
        raise ValueError("The evaluation brief IDs must exactly match the gold case IDs")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output_root = args.output or PROJECT_ROOT / "runs" / f"eval-{stamp}-{args.mode}"
    output_root.mkdir(parents=True, exist_ok=True)
    report_path = output_root / "report.json"
    if report_path.exists():
        raise FileExistsError(f"Evaluation report already exists: {report_path}")

    bundle_paths: list[Path] = []
    execution_errors: list[dict[str, str]] = []
    for brief in briefs:
        try:
            result, run_dir = await run_brief(
                brief, mode=args.mode, knowledge_path=args.knowledge,
                rules_path=args.rules, output_root=output_root,
            )
            bundle_paths.append(run_dir / "bundle.json")
            print(f"{brief.id}: {result.status}")
        except Exception as exc:
            # Keep the attempted brief in every denominator even when no normal
            # bundle could be produced. Do not persist exception text or secrets.
            error_type = type(exc).__name__
            error_dir = output_root / f"eval-run-error-{brief.id}"
            error_dir.mkdir(exist_ok=False)
            error_bundle = error_dir / "bundle.json"
            error_bundle.write_text(json.dumps({
                "run_id": error_dir.name,
                "brief": brief.model_dump(),
                "status": "run_error",
                "facts": [], "draft": None,
                "checks": {"passed": False, "reason": "eval_runner_exception", "error_type": error_type},
                "generation": None, "trace": [],
                "eval_runner_generated_failure_bundle": True,
            }, ensure_ascii=False, indent=2), encoding="utf-8")
            bundle_paths.append(error_bundle)
            execution_errors.append({"brief_id": brief.id, "error_type": error_type})
            print(f"{brief.id}: run_error ({error_type})")

    report = evaluate_bundles(bundle_paths, args.gold)
    report["batch"] = {
        "mode": args.mode,
        "briefs_path": str(args.briefs),
        "gold_path": str(args.gold),
        "knowledge_path": str(args.knowledge),
        "rules_path": str(args.rules),
        "output_root": str(output_root),
        "execution_errors": execution_errors,
        "offline_template_demo": args.mode == "offline",
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Saved deterministic evaluation -> {report_path}")
    return 0


def _record_review(args: argparse.Namespace) -> int:
    bundle_path = args.run_dir / "bundle.json"
    if not bundle_path.is_file():
        raise FileNotFoundError(f"No bundle.json in {args.run_dir}")
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    if args.decision == "approved" and bundle["status"] != "pending_review":
        raise ValueError("Only a pending_review bundle can be approved")
    review_path = args.run_dir / "editor_decision.json"
    if review_path.exists():
        raise FileExistsError(f"Review already recorded in {review_path}")
    decision = {
        "run_id": bundle["run_id"],
        "decision": args.decision,
        "reviewer": args.reviewer,
        "notes": args.notes,
        "at_utc": datetime.now(timezone.utc).isoformat(),
        "published": False,
    }
    review_path.write_text(json.dumps(decision, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Saved editor decision -> {review_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="growth-agent",
        description="Independent CapCut-public-docs growth content prototype",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "demo"):
        command = sub.add_parser(name)
        command.add_argument("--briefs", type=Path, default=PROJECT_ROOT / "data" / "demo_briefs.jsonl")
        command.add_argument("--knowledge", type=Path, default=DEFAULT_KNOWLEDGE)
        command.add_argument("--rules", type=Path, default=DEFAULT_RULES)
        command.add_argument("--output", type=Path, default=PROJECT_ROOT / "runs")
        command.add_argument(
            "--mode",
            choices=("offline", "qwen", "api"),
            default="qwen" if name == "run" else "offline",
        )
        if name == "run":
            command.add_argument("--id", required=True)
    review = sub.add_parser("review")
    review.add_argument("--run-dir", type=Path, required=True)
    review.add_argument("--decision", choices=("approved", "changes_requested", "rejected"), required=True)
    review.add_argument("--reviewer", required=True)
    review.add_argument("--notes", default="")
    evaluate = sub.add_parser("evaluate", help="Score saved bundles with deterministic gold labels")
    evaluate.add_argument("paths", type=Path, nargs="+", help="Bundle files, run directories, or run roots")
    evaluate.add_argument("--gold", type=Path, default=DEFAULT_EVAL_GOLD)
    evaluate.add_argument("--output", type=Path, help="Write the JSON report here; otherwise print it")
    eval_run = sub.add_parser("eval-run", help="Run the held-out briefs and save a JSON report")
    eval_run.add_argument("--mode", choices=("offline", "api", "qwen"), required=True)
    eval_run.add_argument("--briefs", type=Path, default=DEFAULT_EVAL_BRIEFS)
    eval_run.add_argument("--gold", type=Path, default=DEFAULT_EVAL_GOLD)
    eval_run.add_argument("--knowledge", type=Path, default=DEFAULT_KNOWLEDGE)
    eval_run.add_argument("--rules", type=Path, default=DEFAULT_RULES)
    eval_run.add_argument("--output", type=Path, help="Directory for run bundles and report.json")
    serve = sub.add_parser("serve", help="Start the local editor web interface")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=7860)
    args = parser.parse_args(argv)
    if args.command == "review":
        return _record_review(args)
    if args.command == "evaluate":
        return _evaluate(args)
    if args.command == "eval-run":
        return asyncio.run(_eval_run(args))
    if args.command == "serve":
        import uvicorn

        uvicorn.run("growth_agent.webapp:app", host=args.host, port=args.port)
        return 0
    return asyncio.run(_execute(args))


if __name__ == "__main__":
    raise SystemExit(main())
