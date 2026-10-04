import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from overflow_evidence.cli import main as cli_main

UTC = timezone.utc
NOW = datetime.now(UTC)
OPENED = (NOW - timedelta(hours=3)).isoformat()
RECENT = (NOW - timedelta(hours=2)).isoformat()
EARLY = (NOW - timedelta(hours=5)).isoformat()  # 早于事件建立时间 → 补录


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli_main(list(argv))
    return code, out.getvalue(), err.getvalue()


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "db.json")

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, *argv):
        return run_cli("--db", self.db, *argv)

    def test_full_flow(self):
        code, out, _ = self.cli("create-incident", "INC-1",
                                "--opened-at", OPENED,
                                "--description", "中秋冒溢")
        self.assertEqual(code, 0)

        code, out, _ = self.cli("submit", "INC-1", "--kind", "sensor",
                                "--location", "BLOCK-2",
                                "--observed-at", RECENT,
                                "--payload", '{"metric":"cod","value":120,"limit":40}')
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["evidence_id"], "EV-00001")

        # 补录：早于事件建立时间，无理由被拒；补齐理由与双人确认后入账
        code, _, err = self.cli("submit", "INC-1", "--kind", "sample",
                                "--location", "BLOCK-2",
                                "--observed-at", EARLY,
                                "--payload", '{"analyte":"cod","value":90,"limit":40}')
        self.assertEqual(code, 2)
        self.assertIn("补录", err)
        code, out, _ = self.cli("submit", "INC-1", "--kind", "sample",
                                "--location", "BLOCK-2",
                                "--observed-at", EARLY,
                                "--payload", '{"analyte":"cod","value":90,"limit":40}',
                                "--backfill-reason", "实验室节后补送",
                                "--confirm", "张三", "--confirm", "李四")
        self.assertEqual(code, 0)

        code, out, _ = self.cli("report", "INC-1")
        self.assertEqual(code, 0)
        r1 = json.loads(out)["report_id"]

        code, out, _ = self.cli("withdraw", "EV-00001",
                                "--reason", "传感器漂移", "--by", "值班长")
        self.assertEqual(code, 0)
        code, out, _ = self.cli("report", "INC-1")
        r2 = json.loads(out)["report_id"]
        self.assertNotEqual(r1, r2)

        code, out, _ = self.cli("verify", r2)
        self.assertTrue(json.loads(out)["valid"])

        code, out, _ = self.cli("diff", r1, r2)
        diff = json.loads(out)
        self.assertEqual(diff["evidence"]["withdrawn"], ["EV-00001"])

        code, out, _ = self.cli("evidence-of", r1, "segment:BLOCK-2")
        self.assertEqual(len(json.loads(out)), 2)

        code, out, _ = self.cli("completion", "INC-1:contain:BLOCK-2")
        self.assertIn("condition", json.loads(out))

        code, out, _ = self.cli("timeline", "INC-1")
        self.assertEqual(len(json.loads(out)), 2)

        code, out, _ = self.cli("verify-chain")
        self.assertTrue(json.loads(out)["valid"])

    def test_import_command(self):
        self.cli("create-incident", "INC-1", "--opened-at", OPENED)
        batch = Path(self.tmp.name) / "batch.json"
        batch.write_text(json.dumps([
            {"kind": "inspection", "observed_at": RECENT,
             "location_code": "BLOCK-2", "payload": {"overflow_observed": True}},
            {"kind": "inspection", "observed_at": RECENT,
             "location_code": "BLOCK-2", "payload": {}},
        ]), encoding="utf-8")
        code, out, _ = self.cli("import", "INC-1", "--file", str(batch))
        self.assertEqual(code, 0)
        result = json.loads(out)
        self.assertEqual(result["accepted_count"], 1)
        self.assertEqual(result["rejected_count"], 1)

    def test_error_exit_code(self):
        code, _, err = self.cli("show", "RPT-NOPE-1")
        self.assertEqual(code, 2)
        self.assertIn("error", err)


if __name__ == "__main__":
    unittest.main()
