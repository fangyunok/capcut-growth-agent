"""Per-feature drift alarm across consecutive catalog versions.

Reads the snapshots under ``versions/`` and produces:

* ``versions/drift-report.json`` — structured output: per-feature card
  counts for the latest two snapshots, the change rate, and a list of
  features whose change rate exceeds ``--threshold`` (default 20%).
* ``versions/drift-report.md`` — same data, human-readable summary.

The script is read-only — it never mutates versions or registry. The
operator decides whether to inspect the diff and roll forward.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
VERSIONS_DIR = REPO_ROOT / "versions"


def _load_facts(name: str) -> list[dict]:
    facts_path = VERSIONS_DIR / name / "facts.jsonl"
    if not facts_path.is_file():
        raise FileNotFoundError(f"snapshot {name} missing facts.jsonl")
    return [
        json.loads(line) for line in facts_path.read_text(encoding="utf-8").splitlines() if line
    ]


def _feature_counts(facts: list[dict]) -> dict[str, int]:
    return dict(Counter(f.get("feature", "<unknown>") for f in facts))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--threshold", type=float, default=0.20,
                        help="Per-feature change rate (fraction) above which to alarm.")
    parser.add_argument("--old", default=None,
                        help="Older version name. Defaults to versions[-2].")
    parser.add_argument("--new", default=None,
                        help="Newer version name. Defaults to versions[-1].")
    args = parser.parse_args()

    if not VERSIONS_DIR.is_dir():
        print("no versions/ directory — run scripts/update_corpus.py first.", file=sys.stderr)
        return 1

    snapshots = sorted(p.name for p in VERSIONS_DIR.iterdir() if p.is_dir())
    if len(snapshots) < 2:
        print(f"need >=2 snapshots to compute drift; have {snapshots}", file=sys.stderr)
        return 1

    old = args.old or snapshots[-2]
    new = args.new or snapshots[-1]

    old_counts = _feature_counts(_load_facts(old))
    new_counts = _feature_counts(_load_facts(new))
    all_features = sorted(set(old_counts) | set(new_counts))

    rows: list[dict] = []
    for feature in all_features:
        before = old_counts.get(feature, 0)
        after = new_counts.get(feature, 0)
        denom = max(before, 1)
        delta_pct = (after - before) / denom
        rows.append(
            {
                "feature": feature,
                "before": before,
                "after": after,
                "delta": after - before,
                "delta_pct": round(delta_pct, 4),
                "alarm": abs(delta_pct) >= args.threshold,
            }
        )

    alarmed = [r for r in rows if r["alarm"]]
    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "old": old,
        "new": new,
        "threshold": args.threshold,
        "rows": rows,
        "alarmed_count": len(alarmed),
        "alarmed": alarmed,
    }

    out_json = VERSIONS_DIR / "drift-report.json"
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    md_lines = [
        f"# Drift report ({old} -> {new})",
        "",
        f"Threshold: {args.threshold:.0%}",
        f"Alarmed features: {len(alarmed)} / {len(rows)}",
        "",
        "| feature | before | after | delta | delta % | alarm |",
        "|---|---:|---:|---:|---:|:--:|",
    ]
    for r in rows:
        md_lines.append(
            f"| {r['feature']} | {r['before']} | {r['after']} | "
            f"{r['delta']:+d} | {r['delta_pct']:+.1%} | {'!!' if r['alarm'] else ''} |"
        )
    if alarmed:
        md_lines += ["", "## Alarmed features", ""]
        for r in alarmed:
            md_lines.append(
                f"* **{r['feature']}**: {r['before']} -> {r['after']} "
                f"({r['delta_pct']:+.1%})"
            )
    out_md = VERSIONS_DIR / "drift-report.md"
    out_md.write_text("\n".join(md_lines), encoding="utf-8")

    print(f"[drift] wrote {out_json.name} + {out_md.name}")
    print(f"[drift] {len(alarmed)} alarmed (threshold {args.threshold:.0%})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())