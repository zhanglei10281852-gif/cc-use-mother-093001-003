import json
import tempfile
import unittest
from pathlib import Path

from helpers import make_service


class EventStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = make_service(self.tmp.name)

    def test_append_only_chain_verifies(self):
        self.service.create_incident("INC-1", "冒溢", actor="tester")
        self.service.register_evidence(
            "INC-1", kind="inspection", observed_at="2026-09-29T08:00:00+00:00",
            location_code="L-1", payload={"overflow_observed": True},
            source="环卫", registered_by="甲",
        )
        result = self.service.verify_chain()
        self.assertTrue(result["valid"], result["errors"])
        self.assertEqual(result["checked"], 2)

    def test_tampered_log_is_detected(self):
        self.service.create_incident("INC-1", "冒溢", actor="tester")
        log_path = Path(self.tmp.name) / "events.jsonl"
        lines = log_path.read_text(encoding="utf-8").splitlines()
        event = json.loads(lines[0])
        event["data"]["title"] = "被篡改"
        lines[0] = json.dumps(event, ensure_ascii=False)
        log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        result = self.service.verify_chain()
        self.assertFalse(result["valid"])
        self.assertTrue(any("哈希" in e for e in result["errors"]))

    def test_quarantine_is_separate_from_main_log(self):
        self.service.create_incident("INC-1", "冒溢", actor="tester")
        self.service.import_batch("INC-1", [{"kind": "unknown-kind"}], mode="partial")
        quarantined = self.service.store.quarantined()
        self.assertEqual(len(quarantined), 1)
        # 隔离记录不进入主日志，哈希链不受影响
        self.assertTrue(self.service.verify_chain()["valid"])
        self.assertEqual(self.service.verify_chain()["checked"], 1)


if __name__ == "__main__":
    unittest.main()
