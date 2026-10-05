"""Re-run the OBS corpus + catalog pipeline and emit a versioned snapshot.

What it does
------------
1. Re-runs ``corpus.discover`` + ``corpus.save_documents`` against the
   upstream OBS knowledge base.
3. Calls ``catalog_builder.build_cards`` over the freshly crawled
   documents to produce the fact-card catalog.
4. Compares the new catalog to the previous version recorded in
   ``versions/registry.json`` and emits a ``diff.json`` with the
   added/removed/changed cards.
5. Writes a self-contained snapshot under ``versions/<name>/`` so a
   future deployment can roll back or audit.

Modes
-----
* ``--mode real``   (default) — actually re-crawl from upstream.
* ``--mode fixture``         — copy the bundled
  ``data/audit_knowledge_obs.json`` as the "new" snapshot. Lets the
  pipeline be exercised end-to-end without network access (CI, air-gapped
  environments, demos).

Approval gate
-------------
The script *never* overwrites the currently active version. After writing
the new snapshot, the operator must run ``scripts/approval_gate.py
--sign-by <name> --activate <version>`` to publish it. See
``docs/DATA_UPDATE.md``.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from growth_agent import catalog_builder, corpus  # noqa: E402


VERSIONS_DIR = REPO_ROOT / "versions"
REGISTRY = VERSIONS_DIR / "registry.json"
DATA_DIR = REPO_ROOT / "data"
SEED_URLS: list[str] = [
    "https://obsproject.com/kb/getting-started",
]


# ---------- Registry helpers ----------
def load_registry() -> dict:
    if not REGISTRY.is_file():
        return {"versions": [], "active": None}
    return json.loads(REGISTRY.read_text(encoding="utf-8"))


def save_registry(reg: dict) -> None:
    REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    REGISTRY.write_text(
        json.dumps(reg, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ---------- Snapshot writer ----------
def _next_version_name(reg: dict) -> str:
    existing = {v["name"] for v in reg["versions"]}
    for i in range(1, 1000):
        name = f"v{i}"
        if name not in existing:
            return name
    raise RuntimeError("ran out of version names")


def _write_snapshot(name: str, facts: list[dict], source_urls: list[str]) -> Path:
    snap_dir = VERSIONS_DIR / name
    snap_dir.mkdir(parents=True, exist_ok=True)
    facts_path = snap_dir / "facts.jsonl"
    with facts_path.open("w", encoding="utf-8") as fh:
        for fact in facts:
            fh.write(json.dumps(fact, ensure_ascii=False) + "\n")
    manifest = {
        "name": name,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "fact_count": len(facts),
        "source_url_count": len(source_urls),
        "source_urls": sorted(source_urls),
    }
    (snap_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return snap_dir


# ---------- Diff ----------
def _by_fingerprint(facts: list[dict]) -> dict[str, dict]:
    return {catalog_builder._fingerprint(f["statement"]): f for f in facts}


def compute_diff(old: list[dict], new: list[dict]) -> dict:
    """Return added / removed / changed lists keyed by sentence fingerprint."""
    old_map = _by_fingerprint(old)
    new_map = _by_fingerprint(new)
    added = [new_map[k] for k in new_map if k not in old_map]
    removed = [old_map[k] for k in old_map if k not in new_map]
    changed: list[dict] = []
    for k in new_map:
        if k in old_map and json.dumps(
            new_map[k], ensure_ascii=False, sort_keys=True
        ) != json.dumps(old_map[k], ensure_ascii=False, sort_keys=True):
            changed.append({"old": old_map[k], "new": new_map[k]})
    return {
        "added": added,
        "removed": removed,
        "changed": changed,
        "added_count": len(added),
        "removed_count": len(removed),
        "changed_count": len(changed),
    }


# ---------- Builders ----------
def build_from_fixture(drift_pct: float = 0.0) -> tuple[list[dict], list[str]]:
    """Return (facts, urls) by reading audit_knowledge_obs.json as the source.

    ``drift_pct`` simulates upstream editorial churn by randomly reassigning
    the ``feature`` field on a fraction of the cards (0.0 keeps everything
    identical, 0.05 changes ~5% of cards). This is purely for offline demos
    and tests — real crawls do not depend on this knob.
    """
    import random

    bundle_path = DATA_DIR / "audit_knowledge_obs.json"
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    facts = [dict(f) for f in bundle["facts"]]

    if drift_pct > 0.0:
        rng = random.Random(20251004)
        features = sorted({f.get("feature", "") for f in facts})
        for f in facts:
            if rng.random() < drift_pct and len(features) > 1:
                alt = rng.choice([x for x in features if x != f.get("feature", "")])
                f["feature"] = alt

    urls = sorted({f["source_url"] for f in facts})
    return facts, urls


def build_from_network(max_pages: int = 110) -> tuple[list[dict], list[str]]:
    """Re-crawl the upstream knowledge base and turn the cards into facts."""
    print(f"[update] discovering up to {max_pages} OBS KB pages...", flush=True)
    docs = corpus.discover(SEED_URLS, max_pages=max_pages)
    print(f"[update] crawled {len(docs)} documents", flush=True)

    raw_dir = REPO_ROOT / "data" / "corpus_raw"
    corpus.save_documents(docs, raw_dir)
    facts: list[dict] = []
    for doc in docs:
        cards = catalog_builder.build_cards(doc)
        facts.extend(cards)
    urls = sorted({doc.url for doc in docs})
    return facts, urls


# ---------- Main ----------
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=["real", "fixture"],
        default="fixture",
        help=(
            "real: re-crawl upstream OBS KB. "
            "fixture: copy the bundled knowledge base as the 'new' snapshot. "
            "Default: fixture (offline / safe)."
        ),
    )
    parser.add_argument(
        "--max-pages", type=int, default=110, help="Crawl budget for --mode real."
    )
    parser.add_argument(
        "--drift-pct",
        type=float,
        default=0.0,
        help=(
            "Fixture-only knob: simulate upstream editorial churn by "
            "reassigning the ``feature`` field on this fraction of cards. "
            "Real crawls ignore it."
        ),
    )
    args = parser.parse_args()

    started = time.perf_counter()
    reg = load_registry()
    name = _next_version_name(reg)

    if args.mode == "real":
        facts, urls = build_from_network(max_pages=args.max_pages)
    else:
        facts, urls = build_from_fixture(drift_pct=args.drift_pct)

    snap_dir = _write_snapshot(name, facts, urls)

    # Diff against previous snapshot, if any.
    previous_facts: list[dict] = []
    if reg["versions"]:
        prev = reg["versions"][-1]["name"]
        prev_facts_path = VERSIONS_DIR / prev / "facts.jsonl"
        if prev_facts_path.is_file():
            previous_facts = [
                json.loads(line) for line in prev_facts_path.read_text(encoding="utf-8").splitlines() if line
            ]
    diff = compute_diff(previous_facts, facts)
    (snap_dir / "diff.json").write_text(
        json.dumps(
            {
                "vs_previous": reg["versions"][-1]["name"] if reg["versions"] else None,
                **diff,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    reg["versions"].append(
        {
            "name": name,
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "fact_count": len(facts),
            "added": diff["added_count"],
            "removed": diff["removed_count"],
            "changed": diff["changed_count"],
            "mode": args.mode,
        }
    )
    save_registry(reg)

    elapsed = time.perf_counter() - started
    print(
        f"[update] wrote {name}: facts={len(facts)} "
        f"+{diff['added_count']}/-{diff['removed_count']}/~{diff['changed_count']} "
        f"in {elapsed:.1f}s -> {snap_dir}"
    )
    print(f"[update] activate with: python scripts/approval_gate.py --sign-by <name> --activate {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())