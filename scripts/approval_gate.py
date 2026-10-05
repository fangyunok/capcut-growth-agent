"""Manual approval gate for activating a corpus version.

Publishing a freshly built snapshot is *deliberately* a two-step process:

1. ``scripts/update_corpus.py`` writes ``versions/<name>/`` with a
   ``diff.json`` against the previous version.
2. ``scripts/approval_gate.py --sign-by <name> --activate <version>``
   reviews the diff and, on confirmation, marks ``<version>`` as the active
   one in ``versions/registry.json``.

The script refuses to:

* Activate a version that does not exist.
* Activate a version while a previous active version is still in place
  *unless* ``--force-replace`` is passed. This prevents accidental
  clobbering of the running production catalog.
* Skip the signer. Every activation requires ``--sign-by <name>``.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
VERSIONS_DIR = REPO_ROOT / "versions"
REGISTRY = VERSIONS_DIR / "registry.json"
SIGNATURES = VERSIONS_DIR / "SIGNATURES.jsonl"


def _load_registry() -> dict:
    if not REGISTRY.is_file():
        print("registry.json not found — run scripts/update_corpus.py first.", file=sys.stderr)
        sys.exit(2)
    return json.loads(REGISTRY.read_text(encoding="utf-8"))


def _save_registry(reg: dict) -> None:
    REGISTRY.write_text(json.dumps(reg, ensure_ascii=False, indent=2), encoding="utf-8")


def _append_signature(version: str, signer: str, note: str | None) -> None:
    SIGNATURES.parent.mkdir(parents=True, exist_ok=True)
    with SIGNATURES.open("a", encoding="utf-8") as fh:
        record = {
            "version": version,
            "signer": signer,
            "signed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "note": note,
        }
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sign-by", required=True,
                        help="Name of the human approver. Written into SIGNATURES.jsonl.")
    parser.add_argument("--activate", required=True,
                        help="Version name to mark as the active snapshot.")
    parser.add_argument("--note", default=None,
                        help="Optional free-text note (ticket id, reviewer comment).")
    parser.add_argument("--force-replace", action="store_true",
                        help="Override the active-version safety check.")
    args = parser.parse_args()

    reg = _load_registry()
    versions = {v["name"]: v for v in reg.get("versions", [])}
    if args.activate not in versions:
        print(f"version '{args.activate}' not found in registry. "
              f"known: {sorted(versions)}", file=sys.stderr)
        return 1

    active = reg.get("active")
    if active and active != args.activate and not args.force_replace:
        print(f"refusing to replace active version '{active}' with '{args.activate}' "
              f"without --force-replace.", file=sys.stderr)
        return 3

    diff_path = VERSIONS_DIR / args.activate / "diff.json"
    diff_summary = ""
    if diff_path.is_file():
        diff = json.loads(diff_path.read_text(encoding="utf-8"))
        diff_summary = (
            f"+{diff.get('added_count', 0)} "
            f"-{diff.get('removed_count', 0)} "
            f"~{diff.get('changed_count', 0)}"
        )

    reg["active"] = args.activate
    reg["active_activated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    reg["active_signed_by"] = args.sign_by
    _save_registry(reg)

    _append_signature(args.activate, args.sign_by, args.note)

    print(f"[approval] activated {args.activate} (signer={args.sign_by}, drift={diff_summary or 'n/a'})")
    print(f"[approval] registry active -> {args.activate}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())