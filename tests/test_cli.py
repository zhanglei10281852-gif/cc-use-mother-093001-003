import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "src"))

from overflow_evidence.cli import main as cli_main

RECENT = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()


def run_cli(*argv) -> tuple[int, dict]:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = cli_main(list(argv))
    output = buffer.getvalue()
    return code, (json.loads(output) if output.strip() else {})


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = ["--data-dir", self.tmp.name]

    def test_full_cli_flow(self):
        code, _ = run_cli(*self.base, "init-incident", "--incident-id", "INC-CLI", "--title", "中秋冒溢")
        self.assertEqual(code, 0)
        code, evidence = run_cli(
            *self.base, "add-evidence", "--incident-id", "INC-CLI", "--kind", "sensor",
            "--observed-at", RECENT, "--location", "BLOCK-2",
            "--source", "排水公司", "--by", "平台",
            "--payload", '{"parameter": "cod", "value": 45.0, "threshold": 30.0}',
            "--evidence-id", "E-CLI-1",
        )
        self.assertEqual(code, 0)
        self.assertEqual(evidence["status"], "confirmed")

        code, conclusions = run_cli(*self.base, "conclusions", "--incident-id", "INC-CLI")
        self.assertEqual(code, 0)
        self.assertEqual(conclusions["impact_zones"][0]["zone_id"], "ZONE-BLOCK-2")

        code, report = run_cli(*self.base, "issue-report", "--incident-id", "INC-CLI", "--by", "复盘组")
        self.assertEqual(code, 0)
        self.assertEqual(report["version"], 1)

        code, verification = run_cli(*self.base, "verify-report", "--report-id", report["report_id"])
        self.assertEqual(code, 0)
        self.assertTrue(verification["valid"])

        code, provenance = run_cli(
            *self.base, "provenance", "--report-id", report["report_id"], "--ref", "zone:ZONE-BLOCK-2"
        )
        self.assertEqual(code, 0)
        self.assertEqual([m["evidence_id"] for m in provenance["materials"]], ["E-CLI-1"])

        code, items = run_cli(*self.base, "remediation", "--incident-id", "INC-CLI")
        self.assertEqual(code, 0)
        self.assertTrue(all("completion_conditions" in i for i in items))

        code, chain = run_cli(*self.base, "verify-chain")
        self.assertEqual(code, 0)
        self.assertTrue(chain["valid"])

    def test_cli_error_exit_code(self):
        run_cli(*self.base, "init-incident", "--incident-id", "INC-CLI", "--title", "x")
        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer):
            code = cli_main([*self.base, "confirm", "--evidence-id", "E-NOPE", "--by", "甲"])
        self.assertEqual(code, 2)
        self.assertIn("NotFoundError", buffer.getvalue())

    def test_run_cli_smoke_entrypoint(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "run_cli.py")], capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["evidence_id"], "E-9")

    def test_run_cli_forwards_to_full_cli(self):
        with tempfile.TemporaryDirectory() as data_dir:
            result = subprocess.run(
                [sys.executable, str(ROOT / "run_cli.py"), "--data-dir", data_dir, "verify-chain"],
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(json.loads(result.stdout)["valid"])


if __name__ == "__main__":
    unittest.main()
