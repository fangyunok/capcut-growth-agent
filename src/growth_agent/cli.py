"""Command-line entry point for local runs and editorial decisions."""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .audit import AuditRequest, DEFAULT_AUDIT_KNOWLEDGE, DEFAULT_AUDIT_RULES, run_audit
from .audit_evaluation import evaluate_audits
from .evaluation import evaluate_bundles
from .pipeline import run_brief
from .retrieval import STRATEGIES
from .schemas import Brief
from .resources import (
    DEFAULT_KNOWLEDGE, DEFAULT_RULES, DEFAULT_OUTPUT_ROOT, DEFAULT_DEMO_BRIEFS,
    DEFAULT_EVAL_BRIEFS, DEFAULT_EVAL_GOLD, DEFAULT_AUDIT_CASES, DEFAULT_AUDIT_GOLD, DATA_DIR,
    DEFAULT_AUDIT_KNOWLEDGE_OBS, DEFAULT_RETRIEVAL_QUERIES,
)


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


def load_audit_requests(path: Path) -> list[AuditRequest]:
    if path.suffix == ".jsonl":
        records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    else:
        content = json.loads(path.read_text(encoding="utf-8"))
        records = content if isinstance(content, list) else [content]
    requests = [AuditRequest.model_validate(record) for record in records]
    if len({item.id for item in requests}) != len(requests):
        raise ValueError("Audit request IDs must be unique")
    return requests


async def _audit_execute(args: argparse.Namespace) -> int:
    matches = [item for item in load_audit_requests(args.cases) if item.id == args.id]
    if not matches:
        raise ValueError(f"Audit case {args.id!r} was not found in {args.cases}")
    result, run_dir = await run_audit(
        matches[0], mode=args.mode, strategy=args.strategy, knowledge_path=args.knowledge,
        rules_path=args.rules, output_root=args.output,
    )
    print(f"{args.id}: {result.status} -> {run_dir}")
    return 2 if result.status == "model_error" else 0


async def _audit_eval_execute(args: argparse.Namespace) -> int:
    if args.suite == "curated":
        if args.cases == DEFAULT_AUDIT_CASES:
            args.cases = DATA_DIR / "audit_eval_cases.jsonl"
        if args.gold == DEFAULT_AUDIT_GOLD:
            args.gold = DATA_DIR / "audit_eval_gold.json"
    requests = load_audit_requests(args.cases)
    gold = json.loads(args.gold.read_text(encoding="utf-8"))
    if {item.id for item in requests} != {case["id"] for case in gold["cases"]}:
        raise ValueError("Audit input IDs must match the gold case IDs")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output_root = args.output or DEFAULT_OUTPUT_ROOT / f"audit-eval-{stamp}-{args.mode}-{args.strategy}"
    output_root.mkdir(parents=True, exist_ok=True)
    report_path = output_root / "report.json"
    if report_path.exists():
        raise FileExistsError(f"Evaluation report already exists: {report_path}")
    paths = []
    execution_errors = []
    for request in requests:
        try:
            result, run_dir = await run_audit(
                request, mode=args.mode, strategy=args.strategy, knowledge_path=args.knowledge,
                rules_path=args.rules, output_root=output_root,
            )
            paths.append(run_dir / "audit_bundle.json")
            print(f"{request.id}: {result.status}")
        except Exception as exc:
            error_type = type(exc).__name__
            error_dir = output_root / f"runner-error-{len(paths):04d}"
            error_dir.mkdir(exist_ok=False)
            failure_path = error_dir / "audit_bundle.json"
            failure_path.write_text(json.dumps({
                "run_id": error_dir.name, "request": request.model_dump(),
                "status": "run_error", "proposal": None, "facts": [],
                "revised_checks": {"passed": False, "reason": "eval_runner_exception", "error_type": error_type},
                "trace": [], "eval_runner_generated_failure_bundle": True,
            }, ensure_ascii=False, indent=2), encoding="utf-8")
            paths.append(failure_path)
            execution_errors.append({"id": request.id, "error_type": error_type})
            print(f"{request.id}: run_error ({error_type})")
    report = evaluate_audits(paths, args.gold)
    report["batch"] = {
        "mode": args.mode, "strategy": args.strategy, "suite": args.suite,
        "data_version": gold.get("version"), "execution_errors": execution_errors,
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Saved audit development regression -> {report_path}")
    return 2 if execution_errors or any(case["status"] == "model_error" for case in report["per_case"]) else 0


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
    output_root = args.output or DEFAULT_OUTPUT_ROOT / f"eval-{stamp}-{args.mode}"
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


def _retrieval_eval(args: argparse.Namespace) -> int:
    """Score one retrieval strategy against the curated gold fact IDs."""
    from .retrieval_evaluation import evaluate_retrieval

    report = evaluate_retrieval(
        strategy=args.retrieval,
        cases_path=args.cases,
        gold_path=args.gold,
        knowledge_path=args.knowledge,
        top_k=args.top_k,
    )
    report["batch"] = {
        "retrieval": args.retrieval,
        "top_k": args.top_k,
        "cases_path": str(args.cases),
        "gold_path": str(args.gold),
        "knowledge_path": str(args.knowledge),
    }
    serialized = json.dumps(report, ensure_ascii=False, indent=2)
    if args.report is None:
        print(serialized)
    else:
        if args.report.exists():
            raise FileExistsError(f"Evaluation report already exists: {args.report}")
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(serialized + "\n", encoding="utf-8")
        print(f"Saved retrieval evaluation ({args.retrieval}) -> {args.report}")
    return 0


def _topic_eval(args: argparse.Namespace) -> int:
    """Score topic-level retrieval for naturally written queries."""
    from .retrieval_evaluation import evaluate_feature_retrieval

    report = evaluate_feature_retrieval(
        strategy=args.retrieval,
        queries_path=args.queries,
        knowledge_path=args.knowledge,
        top_k=args.top_k,
    )
    report["batch"] = {
        "retrieval": args.retrieval,
        "top_k": args.top_k,
        "queries_path": str(args.queries),
        "knowledge_path": str(args.knowledge),
    }
    serialized = json.dumps(report, ensure_ascii=False, indent=2)
    if args.report is None:
        print(serialized)
    else:
        if args.report.exists():
            raise FileExistsError(f"Evaluation report already exists: {args.report}")
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(serialized + "\n", encoding="utf-8")
        print(f"Saved topic retrieval evaluation ({args.retrieval}) -> {args.report}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="growth-agent",
        description="Bilingual product marketing claim review with source-backed evidence",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "demo"):
        command = sub.add_parser(name)
        command.add_argument("--briefs", type=Path, default=DEFAULT_DEMO_BRIEFS)
        command.add_argument("--knowledge", type=Path, default=DEFAULT_KNOWLEDGE)
        command.add_argument("--rules", type=Path, default=DEFAULT_RULES)
        command.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_ROOT)
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
    audit = sub.add_parser("audit", help="Review existing EN/ZH marketing copy against product evidence")
    audit.add_argument("--id", required=True)
    audit.add_argument("--cases", type=Path, default=DEFAULT_AUDIT_CASES)
    audit.add_argument("--mode", choices=("qwen", "api"), default="qwen")
    audit.add_argument("--strategy", choices=("agent", "rag"), default="agent")
    audit.add_argument("--knowledge", type=Path, default=DEFAULT_AUDIT_KNOWLEDGE)
    audit.add_argument("--rules", type=Path, default=DEFAULT_AUDIT_RULES)
    audit.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_ROOT)
    audit_eval = sub.add_parser("audit-eval", help="Run development or curated EN/ZH workflow evaluation")
    audit_eval.add_argument("--mode", choices=("qwen", "api"), default="qwen")
    audit_eval.add_argument("--strategy", choices=("agent", "rag"), default="agent")
    audit_eval.add_argument("--suite", choices=("dev", "curated"), default="dev")
    audit_eval.add_argument("--cases", type=Path, default=DEFAULT_AUDIT_CASES)
    audit_eval.add_argument("--gold", type=Path, default=DEFAULT_AUDIT_GOLD)
    audit_eval.add_argument("--knowledge", type=Path, default=DEFAULT_AUDIT_KNOWLEDGE)
    audit_eval.add_argument("--rules", type=Path, default=DEFAULT_AUDIT_RULES)
    audit_eval.add_argument("--output", type=Path)
    retrieval_eval = sub.add_parser(
        "retrieval-eval",
        help="Score one retrieval strategy against the curated gold fact IDs",
    )
    retrieval_eval.add_argument("--retrieval", choices=STRATEGIES, default="lexical")
    retrieval_eval.add_argument("--top-k", type=int, default=5)
    retrieval_eval.add_argument(
        "--cases", type=Path, default=DATA_DIR / "audit_eval_cases.jsonl"
    )
    retrieval_eval.add_argument(
        "--gold", type=Path, default=DATA_DIR / "audit_eval_gold.json"
    )
    retrieval_eval.add_argument("--knowledge", type=Path, default=DEFAULT_AUDIT_KNOWLEDGE)
    retrieval_eval.add_argument("--report", type=Path)
    topic_eval = sub.add_parser(
        "topic-eval",
        help="Score topic-level retrieval for naturally written queries",
    )
    topic_eval.add_argument("--retrieval", choices=STRATEGIES, default="lexical")
    topic_eval.add_argument("--top-k", type=int, default=5)
    topic_eval.add_argument("--queries", type=Path, default=DEFAULT_RETRIEVAL_QUERIES)
    topic_eval.add_argument(
        "--knowledge", type=Path, default=DEFAULT_AUDIT_KNOWLEDGE_OBS
    )
    topic_eval.add_argument("--report", type=Path)
    doctor = sub.add_parser("doctor", help="Check model catalog, bundled data and optional FFmpeg")
    doctor.add_argument("--mode", choices=("qwen", "api"), default="qwen")
    doctor.add_argument("--timeout", type=int, default=5)
    approve = sub.add_parser("approve-audit", help="Confirm the exact revised text after checking its sources")
    approve.add_argument("--run-dir", type=Path, required=True)
    approve.add_argument("--reviewer", required=True)
    approve.add_argument("--copy-version", help="Expected SHA256 of the revised copy, for stale-version checks")
    approve.add_argument("--audit-digest", help="Expected SHA256 of the reviewed audit_bundle.json source snapshot")
    approve.add_argument("--notes", default="")
    storyboard = sub.add_parser("storyboard", help="Make three template segments from approved audit text")
    storyboard.add_argument("--run-dir", type=Path, required=True)
    storyboard.add_argument("--assets", type=Path, required=True, help="JSON array of asset_id, path and kind")
    storyboard.add_argument("--duration", type=float, default=15)
    storyboard.add_argument("--output", type=Path, required=True, help="New storyboard JSON file")
    render = sub.add_parser("render-video", help="Render an exact-text storyboard with real FFmpeg")
    render.add_argument("--storyboard", type=Path, required=True)
    render.add_argument("--asset-root", type=Path, required=True)
    render.add_argument("--audio", type=Path, help="Optional user-supplied audio; no TTS is implied")
    render.add_argument("--font", type=Path)
    render.add_argument("--ffmpeg")
    render.add_argument("--timeout", type=int, default=120)
    render.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_ROOT)
    media_demo_parser = sub.add_parser("media-demo", help="Render a clearly labeled placeholder template without a model")
    media_demo_parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_ROOT)
    media_demo_parser.add_argument("--ffmpeg")
    serve = sub.add_parser("serve", help="Start the local editor web interface")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=7860)
    args = parser.parse_args(argv)
    if args.command == "doctor":
        from .diagnostics import diagnose
        if not 1 <= args.timeout <= 30:
            parser.error("doctor timeout must be from 1 to 30 seconds")
        report = diagnose(mode=args.mode, timeout=args.timeout)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["model_ready"] and report["sample_data_ready"] else 2
    if args.command == "approve-audit":
        from .approval import approve_audit
        path = approve_audit(args.run_dir, reviewer=args.reviewer, expected_version=args.copy_version,
                             expected_audit_digest=args.audit_digest, notes=args.notes)
        print(f"Saved explicit editorial confirmation -> {path}")
        return 0
    if args.command == "storyboard":
        from .approval import load_approved_copy
        from .media import MediaAsset, build_template_storyboard
        records = json.loads(args.assets.read_text(encoding="utf-8"))
        if not isinstance(records, list):
            raise ValueError("Asset manifest must be a JSON array")
        board = build_template_storyboard(load_approved_copy(args.run_dir), [MediaAsset.from_dict(item) for item in records], duration_seconds=args.duration)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as handle:
            json.dump(board.to_dict(), handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        print(f"Saved exact-text template storyboard -> {args.output}")
        return 0
    if args.command == "render-video":
        from .media import Storyboard, render_video
        board = Storyboard.from_dict(json.loads(args.storyboard.read_text(encoding="utf-8")))
        metadata, run_dir = render_video(board, output_root=args.output, asset_root=args.asset_root, audio_path=args.audio, font_path=args.font, ffmpeg=args.ffmpeg, timeout_seconds=args.timeout)
        print(f"{metadata['status']} -> {run_dir}")
        return 0 if metadata["status"] == "completed" else 2
    if args.command == "media-demo":
        from .demo_media import media_demo
        metadata, run_dir = media_demo(args.output, ffmpeg=args.ffmpeg)
        print(f"Template placeholder media demo (no model/TTS): {metadata['status']} -> {run_dir}")
        return 0 if metadata["status"] == "completed" else 2
    if args.command == "review":
        return _record_review(args)
    if args.command == "evaluate":
        return _evaluate(args)
    if args.command == "eval-run":
        return asyncio.run(_eval_run(args))
    if args.command == "audit":
        return asyncio.run(_audit_execute(args))
    if args.command == "audit-eval":
        return asyncio.run(_audit_eval_execute(args))
    if args.command == "retrieval-eval":
        return _retrieval_eval(args)
    if args.command == "topic-eval":
        return _topic_eval(args)
    if args.command == "serve":
        import uvicorn

        uvicorn.run("growth_agent.webapp:app", host=args.host, port=args.port)
        return 0
    return asyncio.run(_execute(args))


if __name__ == "__main__":
    raise SystemExit(main())
