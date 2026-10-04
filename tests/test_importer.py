import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from overflow_evidence.service import IncidentService

UTC = timezone.utc
T0 = datetime(2026, 9, 29, 8, 0, tzinfo=UTC)


def make_service():
    svc = IncidentService(clock=lambda: T0 + timedelta(hours=10))
    svc.create_incident("INC-1", T0)
    svc.submit_evidence("INC-1", kind="sensor", observed_at=T0 + timedelta(hours=1),
                        location_code="BLOCK-1",
                        payload={"metric": "cod", "value": 99.0, "limit": 40.0})
    return svc


class ImportTests(unittest.TestCase):
    def test_bad_records_do_not_pollute_confirmed_data(self):
        svc = make_service()
        result = svc.import_batch("INC-1", [
            {"kind": "sample", "observed_at": (T0 + timedelta(hours=2)).isoformat(),
             "location_code": "BLOCK-2",
             "payload": {"analyte": "cod", "value": 80.0, "limit": 40.0}},
            {"kind": "sample", "observed_at": (T0 + timedelta(hours=3)).isoformat(),
             "location_code": "BLOCK-2",
             "payload": {"analyte": "cod", "value": 80.0}},  # 缺 limit
            {"kind": "unknown_kind", "observed_at": (T0 + timedelta(hours=4)).isoformat(),
             "location_code": "BLOCK-2", "payload": {}},
            {"kind": "sensor", "observed_at": "2026-09-29 10:00",  # 无时区
             "location_code": "BLOCK-2",
             "payload": {"metric": "cod", "value": 1.0, "limit": 40.0}},
            "not-a-dict",
        ])
        self.assertEqual(len(result.accepted), 1)
        self.assertEqual(len(result.quarantine), 4)
        # 台账中只有导入前 1 条 + 新接受 1 条
        self.assertEqual(len(svc.ledger.records("INC-1")), 2)
        self.assertTrue(svc.verify_chain())
        # 隔离区带索引与错误原因
        for q in result.quarantine:
            self.assertTrue(q.errors)

    def test_backfill_rules_apply_inside_batch(self):
        svc = make_service()
        result = svc.import_batch("INC-1", [
            {"kind": "sample", "observed_at": (T0 - timedelta(hours=5)).isoformat(),
             "location_code": "BLOCK-2",
             "payload": {"analyte": "cod", "value": 80.0, "limit": 40.0}},
            {"kind": "sample", "observed_at": (T0 - timedelta(hours=5)).isoformat(),
             "location_code": "BLOCK-2",
             "payload": {"analyte": "cod", "value": 80.0, "limit": 40.0},
             "backfill": {"reason": "实验室节后补送", "confirmed_by": ["张三", "李四"]}},
        ])
        self.assertEqual(len(result.accepted), 1)
        self.assertEqual(len(result.quarantine), 1)
        self.assertIn("补录", result.quarantine[0].errors[0])

    def test_duplicate_ids_quarantined_not_fatal(self):
        svc = make_service()
        result = svc.import_batch("INC-1", [
            {"evidence_id": "B-1", "kind": "inspection",
             "observed_at": (T0 + timedelta(hours=2)).isoformat(),
             "location_code": "BLOCK-2", "payload": {"overflow_observed": True}},
            {"evidence_id": "B-1", "kind": "inspection",
             "observed_at": (T0 + timedelta(hours=3)).isoformat(),
             "location_code": "BLOCK-2", "payload": {"overflow_observed": True}},
        ])
        self.assertEqual(len(result.accepted), 1)
        self.assertEqual(len(result.quarantine), 1)


if __name__ == "__main__":
    unittest.main()
