"""Read-only sample inputs and writable run paths for checkouts and wheels.

Source checkouts read the editable top-level ``data`` directory. A normal pip
installation reads the sample files shipped inside ``growth_agent/data`` and
writes outputs under the caller's working directory, never into site-packages.
"""

from __future__ import annotations

import os
from pathlib import Path


_PACKAGE_ROOT = Path(__file__).resolve().parent
_CHECKOUT_CANDIDATE = _PACKAGE_ROOT.parents[1]
_IN_CHECKOUT = (
    _PACKAGE_ROOT.parent.name == "src"
    and (_CHECKOUT_CANDIDATE / "pyproject.toml").is_file()
    and (_CHECKOUT_CANDIDATE / "data").is_dir()
)

PROJECT_ROOT = _CHECKOUT_CANDIDATE if _IN_CHECKOUT else Path.cwd()
PACKAGED_DATA_DIR = _PACKAGE_ROOT / "data"
DATA_DIR = PROJECT_ROOT / "data" if _IN_CHECKOUT else PACKAGED_DATA_DIR
DEFAULT_OUTPUT_ROOT = Path(os.getenv("GROWTH_RUNS_DIR", str(PROJECT_ROOT / "runs"))).expanduser().resolve()


def data_path(filename: str) -> Path:
    """Locate a bundled sample file; callers pass custom paths separately."""
    if not filename or Path(filename).name != filename or filename in {".", ".."}:
        raise ValueError("Sample data filename must be a single basename")
    target = DATA_DIR / filename
    if not target.is_file():
        raise FileNotFoundError(f"Sample data file is missing: {target}")
    return target


DEFAULT_KNOWLEDGE = data_path("knowledge.json")
DEFAULT_RULES = data_path("editorial_rules.json")
DEFAULT_DEMO_BRIEFS = data_path("demo_briefs.jsonl")
DEFAULT_EVAL_BRIEFS = data_path("eval_briefs.jsonl")
DEFAULT_EVAL_GOLD = data_path("eval_gold.json")
DEFAULT_AUDIT_KNOWLEDGE = data_path("audit_knowledge.json")
DEFAULT_AUDIT_RULES = data_path("audit_rules.json")
DEFAULT_AUDIT_CASES = data_path("audit_cases.jsonl")
DEFAULT_AUDIT_GOLD = data_path("audit_gold.json")
DEFAULT_AUDIT_EVAL_CASES = data_path("audit_eval_cases.jsonl")
DEFAULT_AUDIT_EVAL_GOLD = data_path("audit_eval_gold.json")
DEFAULT_AUDIT_KNOWLEDGE_OBS = data_path("audit_knowledge_obs.json")
DEFAULT_RETRIEVAL_QUERIES = data_path("retrieval_queries.jsonl")
