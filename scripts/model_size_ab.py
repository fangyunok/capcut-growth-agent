"""Run the curated + test audit suites against two Qwen model sizes and write
a side-by-side comparison report.

Prerequisites
-------------
* The remote Ollama instance must be reachable through the SSH tunnel set
  up by ``scripts/autodl_model_setup.sh`` (or directly at ``$GROWTH_QWEN_BASE``).
* Both ``qwen3:4b-instruct`` and ``qwen3:14b`` must be installed.

Usage
-----
::

    python scripts/model_size_ab.py
        --base http://127.0.0.1:11435/v1
        --small qwen3:4b-instruct
        --large qwen3:14b
        --output reports/model-size-ab.json

The script runs each (suite, model) combination sequentially. For the
default curated + test suites (20 cases each), expect ~3-5 minutes on a
RTX 3090 with a warmed-up Ollama cache.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"
sys.path.insert(0, str(SRC))

from growth_agent.cli import _audit_eval_execute  # noqa: E402
from growth_agent.resources import DATA_DIR  # noqa: E402
import argparse  # noqa: E402  (kept for clarity)


SUITES: list[tuple[str, Path, Path]] = [
    ("curated", DATA_DIR / "audit_eval_cases.jsonl", DATA_DIR / "audit_eval_gold.json"),
    ("test",    DATA_DIR / "audit_test_cases.jsonl", DATA_DIR / "audit_test_gold.json"),
]


def _build_argparse(suite: str, cases: Path, gold: Path, model: str, output: Path) -> argparse.Namespace:
    return argparse.Namespace(
        suite=suite,
        cases=cases,
        gold=gold,
        # Use the curated card catalog whose `feature` names match the audit
        # suites. The 690-card OBS catalog uses URL-derived feature slugs
        # (e.g. `advanced-recording-settings-guide`) that never equal the brief
        # feature (`recording-setup`), so the agent loop's exact-feature gate
        # would filter every retrieved card and force `insufficient_evidence`.
        knowledge=DATA_DIR / "audit_knowledge.json",
        rules=DATA_DIR / "audit_rules.json",
        mode="qwen",
        strategy="agent",
        output=output,
    )


async def _run_one(suite: str, model: str, base: str, output_root: Path) -> dict:
    cases, gold = SUITES[next(i for i, (s, _, _) in enumerate(SUITES) if s == suite)][1:]
    output_root.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    os.environ["GROWTH_QWEN_BASE"] = base
    os.environ["GROWTH_QWEN_MODEL"] = model
    args = _build_argparse(suite, cases, gold, model, output_root)
    rc = await _audit_eval_execute(args)
    duration = time.perf_counter() - started
    report_path = output_root / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    return {
        "suite": suite,
        "model": model,
        "duration_s": round(duration, 1),
        "return_code": rc,
        "report_path": str(report_path),
        "summary": {
            "workflow_success": sum(
                1 for c in report.get("per_case", []) if c.get("workflow_success")
            ),
            "correct_abstention": report.get("correct_abstention", {}),
            "required_risk_removed": report.get("required_risk_phrases_removed", {}),
            "total": len(report.get("per_case", [])),
            "workflow_success_rate": (
                round(
                    100 * sum(1 for c in report.get("per_case", []) if c.get("workflow_success"))
                    / max(1, len(report.get("per_case", []))),
                    1,
                )
            ),
            "by_failure": _failure_breakdown(report),
        },
    }


def _failure_breakdown(report: dict) -> dict[str, int]:
    counts: dict[str, int] = {}
    for case in report.get("per_case", []):
        if case.get("workflow_success"):
            continue
        # Surface both the runtime status and any unfilled gate (citations,
        # remaining required removals, etc.) so the breakdown is informative.
        reasons: list[str] = []
        if case.get("status"):
            reasons.append(f"status={case['status']}")
        for removal in case.get("remaining_required_removals", []) or []:
            reasons.append(f"removal={removal}")
        if not case.get("correct_abstention") and case.get("expected_abstention"):
            reasons.append("abstention_missed")
        if not case.get("citation_structure", {}).get("valid"):
            reasons.append("citation_invalid")
        key = "|".join(reasons) or "unknown"
        counts[key] = counts.get(key, 0) + 1
    return counts


async def main_async(base: str, small: str, large: str, output: Path, run_root: Path) -> int:
    runs = [
        ("curated", small, run_root / f"curated-{small.replace(':', '-')}"),
        ("test",    small, run_root / f"test-{small.replace(':', '-')}"),
        ("curated", large, run_root / f"curated-{large.replace(':', '-')}"),
        ("test",    large, run_root / f"test-{large.replace(':', '-')}"),
    ]
    results: list[dict] = []
    for suite, model, out_dir in runs:
        print(f"\n[ab] >>> {suite} / {model}", flush=True)
        result = await _run_one(suite, model, base, out_dir)
        results.append(result)
        print(
            f"[ab] workflow_success={result['summary']['workflow_success_rate']}% "
            f"({result['summary']['workflow_success']}/{result['summary']['total']}) "
            f"in {result['duration_s']}s",
            flush=True,
        )

    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "base_url": base,
        "models": {"small": small, "large": large},
        "results": results,
        "delta": _compute_delta(results, small, large),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\n[ab] wrote {output}")
    return 0


def _compute_delta(results: list[dict], small: str, large: str) -> dict:
    def _by(results, model, suite):
        for r in results:
            if r["model"] == model and r["suite"] == suite:
                return r
        return None

    out: dict[str, dict] = {}
    for suite in ("curated", "test"):
        s = _by(results, small, suite)
        l = _by(results, large, suite)
        if not s or not l:
            continue
        out[suite] = {
            "small_workflow_success": s["summary"]["workflow_success_rate"],
            "large_workflow_success": l["summary"]["workflow_success_rate"],
            "delta_pp": round(l["summary"]["workflow_success_rate"] - s["summary"]["workflow_success_rate"], 1),
            "small_duration_s": s["duration_s"],
            "large_duration_s": l["duration_s"],
            "small_failures": s["summary"]["by_failure"],
            "large_failures": l["summary"]["by_failure"],
        }
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default=os.getenv("GROWTH_QWEN_BASE", "http://127.0.0.1:11435/v1"))
    parser.add_argument("--small", default="qwen3:4b-instruct")
    parser.add_argument("--large", default="qwen3:14b")
    parser.add_argument("--output", type=Path, default=REPO_ROOT / "reports" / "model-size-ab.json")
    parser.add_argument("--run-root", type=Path, default=REPO_ROOT / "runs" / "w3-model-size-ab")
    args = parser.parse_args()
    return asyncio.run(main_async(args.base, args.small, args.large, args.output, args.run_root))


if __name__ == "__main__":
    raise SystemExit(main())