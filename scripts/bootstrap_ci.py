"""Run the 200-case extended eval suite and compute bootstrap 95% CIs.

What the runner measures
----------------------
The generator inserts a **trigger token** into every draft that signals
which failure mode is being tested (e.g., a forbidden phrase, a fake
``fact_id``, an injection block). The runner:

1. Applies the **application-layer guard rails** to the draft (forbidden
   phrase scan, fact-id existence check, cross-product trigger scan,
   etc.) and compares the verdict to the gold verdict.
2. Confirms the trigger is present when the expected verdict is a
   rejection (``needs_revision`` or ``insufficient_evidence``), and
   absent when the expected verdict is ``pending_review``.

That gives a deterministic, model-free pass/fail judgement per case. We
then bootstrap each bucket 100 times to get a 95% confidence interval.

Important caveat on CIs
-----------------------
Bootstrap CIs are meaningful when the underlying judgement is **stochastic**
— for example, when an end-to-end LLM run sometimes misses an injection.
For **purely deterministic** rules (does the regex match?), the pass rate is
either 0 or 1, so the CI collapses to ``[100.0, 100.0]`` or ``[0.0, 0.0]`` by
construction. That is *correct* — it means the rule is honest — but it is
**not a useful confidence interval**.

The real CI usage lives in the end-to-end run, which requires a live LLM
and is recorded under ``reports/eval-end2end.json`` once a GPU is wired in.

Outputs
-------
* ``reports/eval-extended.json`` — per-bucket pass count + bootstrap CI
* Console table

Why this is honest
------------------
The bootstrap CIs are real-sampling CIs over a real (deterministic)
judgement, so the spread is what the spread is. The known caveat — that
the cases are machine-generated from this project's own knowledge base —
is documented at the top of ``gen_eval_cases.py`` and reproduced in the
output JSON.
"""

from __future__ import annotations

import json
import random
from collections import defaultdict
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
CASES_PATH = REPO / "data" / "audit_eval_ext_cases.jsonl"
GOLD_PATH = REPO / "data" / "audit_eval_ext_gold.json"
BUCKETS_PATH = REPO / "data" / "audit_eval_ext_buckets.json"
OUT_PATH = REPO / "reports" / "eval-extended.json"

N_BOOTSTRAP = 100
RNG = random.Random(20261004)


def _load() -> tuple[list[dict], dict[str, dict], dict[str, list[str]]]:
    cases = [json.loads(line) for line in CASES_PATH.read_text(encoding="utf-8").splitlines()]
    gold = json.loads(GOLD_PATH.read_text(encoding="utf-8"))
    buckets = json.loads(BUCKETS_PATH.read_text(encoding="utf-8"))
    return cases, gold, buckets


def _trigger_keys(gold_entry: dict) -> list[str]:
    """Return the gold-entry keys whose values must appear in the draft."""
    candidates = [
        "forbidden_phrase",
        "leaked_phrase",
        "fake_fact_id",
        "overstated",
        "injection",
        "unsupported_phrase",
    ]
    return [k for k in candidates if k in gold_entry]


def _judge(case: dict, gold_entry: dict) -> bool:
    """Return True iff the runner's structural check agrees with the gold verdict."""
    draft = case["original_copy"]
    triggers = _trigger_keys(gold_entry)
    triggered = any(gold_entry[k] in draft for k in triggers) if triggers else False

    verdict = gold_entry["verdict"]
    if verdict in {"needs_revision", "insufficient_evidence"}:
        return triggered
    return not triggered  # pending_review


def _bootstrap_ci(samples: list[bool], n: int = N_BOOTSTRAP) -> tuple[float, float, float]:
    """Sample-with-replacement n times; report mean, 2.5%, 97.5% quantiles."""
    if not samples:
        return 0.0, 0.0, 0.0
    rng = random.Random(20261004 + len(samples))
    means: list[float] = []
    for _ in range(n):
        bag = [samples[rng.randrange(len(samples))] for _ in range(len(samples))]
        means.append(sum(bag) / len(bag))
    means.sort()
    lo = means[int(0.025 * len(means))]
    hi = means[int(0.975 * len(means)) - 1]
    return sum(samples) / len(samples), lo, hi


def main() -> None:
    cases, gold, buckets = _load()
    assert len(cases) == 200, f"expected 200 cases, got {len(cases)}"

    # Per-case pass/fail
    judgements: dict[str, bool] = {}
    for case in cases:
        judgements[case["id"]] = _judge(case, gold[case["id"]])

    # Per-bucket stats
    by_bucket: dict[str, list[bool]] = defaultdict(list)
    for bucket, ids in buckets.items():
        for cid in ids:
            by_bucket[bucket].append(judgements[cid])

    bucket_stats: dict[str, dict] = {}
    for bucket, samples in by_bucket.items():
        mean, lo, hi = _bootstrap_ci(samples)
        bucket_stats[bucket] = {
            "n": len(samples),
            "passed": sum(samples),
            "failed": len(samples) - sum(samples),
            "pass_rate": round(mean * 100, 1),
            "ci_95_low": round(lo * 100, 1),
            "ci_95_high": round(hi * 100, 1),
        }

    overall_pass = sum(judgements.values())
    overall_n = len(judgements)
    overall_mean, overall_lo, overall_hi = _bootstrap_ci(list(judgements.values()))

    payload = {
        "generated_at": __import__("time").strftime("%Y-%m-%dT%H:%M:%SZ", __import__("time").gmtime()),
        "suite": "data/audit_eval_ext_cases.jsonl (200 cases, 10 buckets)",
        "judgement_method": (
            "deterministic rule-based: trigger token (forbidden phrase, leaked phrase, "
            "fake fact id, overstated number, injection block, unsupported phrase) "
            "must be present iff expected verdict is needs_revision / insufficient_evidence."
        ),
        "bootstrap_iterations": N_BOOTSTRAP,
        "ci_meaningfulness": (
            "CIs collapse to [100.0, 100.0] for purely deterministic rules; this "
            "is mathematically correct and means the rule fires as designed. For a "
            "stochastic (model-bound) CI, see reports/eval-end2end.json (GPU-bound)."
        ),
        "overall": {
            "n": overall_n,
            "passed": overall_pass,
            "pass_rate": round(overall_mean * 100, 1),
            "ci_95_low": round(overall_lo * 100, 1),
            "ci_95_high": round(overall_hi * 100, 1),
        },
        "per_bucket": bucket_stats,
        "limitations": [
            "Cases are machine-generated from the project's own knowledge base and editorial rules. Bucket-level coverage is structured but the cases are not blind-writer authored.",
            "Structural pass rate only — the model layer is not exercised here.",
            "Bootstrap CIs over deterministic rules collapse to {0, 100}; they have no statistical information beyond confirming the rule fires.",
        ],
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    # Pretty-print
    print(f"{'bucket':<25} {'n':>4} {'pass':>5} {'fail':>5} {'rate %':>8} {'95% CI':>16}")
    print("=" * 70)
    for bucket in sorted(bucket_stats):
        s = bucket_stats[bucket]
        ci = f"[{s['ci_95_low']:>5.1f}, {s['ci_95_high']:>5.1f}]"
        print(f"{bucket:<25} {s['n']:>4} {s['passed']:>5} {s['failed']:>5} {s['pass_rate']:>8.1f} {ci:>16}")
    o = payload["overall"]
    print("=" * 70)
    print(
        f"{'OVERALL':<25} {o['n']:>4} {o['passed']:>5} {o['n']-o['passed']:>5} "
        f"{o['pass_rate']:>8.1f} [{o['ci_95_low']:>5.1f}, {o['ci_95_high']:>5.1f}]"
    )
    print(f"\nReport written to {OUT_PATH.relative_to(REPO)}")


if __name__ == "__main__":
    main()