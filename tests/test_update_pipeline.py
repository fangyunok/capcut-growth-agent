"""Tests for the data-update pipeline (update_corpus + drift_alert + approval_gate).

Each test runs against an isolated temporary ``versions/`` directory so the
real one isn't touched. The fixture mode of ``update_corpus`` is used so
the tests don't depend on network access.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"


def _isolate_versions(monkey_dir: Path) -> None:
    """Swap the repo's versions/ for a fresh tmp one for the test."""
    versions = REPO_ROOT / "versions"
    if versions.exists():
        shutil.move(str(versions), str(monkey_dir / "versions.bak"))


def _restore_versions(monkey_dir: Path) -> None:
    versions = REPO_ROOT / "versions"
    if versions.exists():
        shutil.rmtree(versions)
    backup = monkey_dir / "versions.bak"
    if backup.exists():
        shutil.move(str(backup), str(versions))


class UpdatePipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile_dir())
        _isolate_versions(self.tmp)

    def tearDown(self) -> None:
        _restore_versions(self.tmp)

    def test_update_corpus_writes_snapshot(self) -> None:
        result = subprocess.run(
            [sys.executable, str(SCRIPTS / "update_corpus.py"), "--drift-pct", "0"],
            capture_output=True, text=True, check=True,
        )
        self.assertIn("wrote v1", result.stdout)
        versions = REPO_ROOT / "versions"
        self.assertTrue((versions / "v1" / "facts.jsonl").is_file())
        self.assertTrue((versions / "v1" / "manifest.json").is_file())
        self.assertTrue((versions / "v1" / "diff.json").is_file())
        reg = json.loads((versions / "registry.json").read_text(encoding="utf-8"))
        self.assertEqual(len(reg["versions"]), 1)
        self.assertEqual(reg["active"], None)

    def test_drift_alert_detects_drift(self) -> None:
        for drift in (0.0, 0.05):
            subprocess.run(
                [sys.executable, str(SCRIPTS / "update_corpus.py"),
                 "--drift-pct", str(drift)],
                capture_output=True, text=True, check=True,
            )
        result = subprocess.run(
            [sys.executable, str(SCRIPTS / "drift_alert.py")],
            capture_output=True, text=True, check=True,
        )
        versions = REPO_ROOT / "versions"
        report = json.loads((versions / "drift-report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["old"], "v1")
        self.assertEqual(report["new"], "v2")
        # 5% drift should alarm at least one feature.
        self.assertGreater(report["alarmed_count"], 0)

    def test_approval_gate_refuses_overwrite_without_force(self) -> None:
        subprocess.run(
            [sys.executable, str(SCRIPTS / "update_corpus.py"), "--drift-pct", "0"],
            capture_output=True, text=True, check=True,
        )
        subprocess.run(
            [sys.executable, str(SCRIPTS / "update_corpus.py"), "--drift-pct", "0.02"],
            capture_output=True, text=True, check=True,
        )
        # Activate v1.
        subprocess.run(
            [sys.executable, str(SCRIPTS / "approval_gate.py"),
             "--sign-by", "reviewer-a", "--activate", "v1"],
            capture_output=True, text=True, check=True,
        )
        # Trying to activate v2 without --force-replace must fail with exit 3.
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "approval_gate.py"),
             "--sign-by", "reviewer-a", "--activate", "v2"],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 3)
        self.assertIn("--force-replace", proc.stderr)
        # With --force-replace it succeeds.
        proc2 = subprocess.run(
            [sys.executable, str(SCRIPTS / "approval_gate.py"),
             "--sign-by", "reviewer-b", "--activate", "v2",
             "--force-replace", "--note", "approved for production"],
            capture_output=True, text=True, check=True,
        )
        reg = json.loads((REPO_ROOT / "versions" / "registry.json").read_text(encoding="utf-8"))
        self.assertEqual(reg["active"], "v2")
        self.assertEqual(reg["active_signed_by"], "reviewer-b")
        sigs = (REPO_ROOT / "versions" / "SIGNATURES.jsonl").read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(sigs), 2)

    def test_approval_gate_rejects_unknown_version(self) -> None:
        subprocess.run(
            [sys.executable, str(SCRIPTS / "update_corpus.py"), "--drift-pct", "0"],
            capture_output=True, text=True, check=True,
        )
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "approval_gate.py"),
             "--sign-by", "reviewer-a", "--activate", "v999"],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 1)


def tempfile_dir() -> Path:
    import tempfile
    return Path(tempfile.mkdtemp())


if __name__ == "__main__":
    unittest.main()