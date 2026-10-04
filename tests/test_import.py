import tempfile
import unittest

from helpers import INCIDENT, make_service


def valid_sensor(evidence_id, observed="2026-09-29T08:00:00+00:00"):
    return {
        "evidence_id": evidence_id,
        "kind": "sensor",
        "observed_at": observed,
        "location_code": "BLOCK-2",
        "payload": {"parameter": "cod", "value": 45.0, "threshold": 30.0},
        "source": "排水公司",
        "registered_by": "平台",
    }


class BatchImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = make_service(self.tmp.name)
        self.service.create_incident(INCIDENT, "中秋冒溢", actor="管理方")
        self.service.register_evidence(INCIDENT, **valid_sensor("E-EXISTING"))

    def mixed_batch(self):
        return [
            valid_sensor("E-OK"),
            {"kind": "sensor", "observed_at": "2026-09-29T08:00:00+00:00",
             "payload": {"parameter": "cod", "value": 1.0}, "source": "排水公司",
             "registered_by": "平台"},  # 缺 location_code
            {"kind": "unknown", "observed_at": "2026-09-29T08:00:00+00:00", "location_code": "B",
             "payload": {}, "source": "x", "registered_by": "y"},  # 未知类型
            {"kind": "sample", "observed_at": "2026-09-29T08:00:00+00:00", "location_code": "B",
             "payload": {"parameter": "cod", "value": 90.0}, "source": "排水公司",
             "registered_by": "化验员"},  # 样本缺 limit
            valid_sensor("E-EXISTING"),  # 与已确认数据重复
            {"kind": "inspection", "observed_at": "2026-09-27T08:00:00+00:00",
             "location_code": "BLOCK-2", "payload": {"overflow_observed": True},
             "source": "环卫", "registered_by": "甲"},  # 补录无理由
            {"evidence_id": "E-BF", "kind": "inspection", "observed_at": "2026-09-27T08:00:00+00:00",
             "location_code": "BLOCK-2", "payload": {"overflow_observed": True},
             "source": "环卫", "registered_by": "甲", "backfill_reason": "纸质记录后补",
             "confirmations": ["乙", "丙"]},  # 合法补录 + 双人确认
            {"kind": "inspection", "observed_at": "2026-09-27T08:00:00+00:00",
             "location_code": "BLOCK-2", "payload": {"overflow_observed": True},
             "source": "环卫", "registered_by": "甲", "backfill_reason": "补录",
             "confirmations": ["甲", "乙"]},  # 登记人自我确认，非法
        ]

    def test_partial_import_quarantines_bad_records(self):
        result = self.service.import_batch(INCIDENT, self.mixed_batch(), mode="partial")
        self.assertTrue(result["committed"])
        self.assertEqual(result["imported"], 2)
        self.assertEqual(result["quarantined"], 6)
        # 坏记录全部进入隔离区并附原因
        quarantined = self.service.store.quarantined()
        self.assertEqual(len(quarantined), 6)
        self.assertTrue(all(q["errors"] for q in quarantined))
        # 已确认数据未被污染：重复编号未被覆盖
        existing = self.service.get_evidence("E-EXISTING")
        self.assertEqual(existing["status"], "confirmed")
        # 合法补录随批双人确认后直接生效
        self.assertEqual(self.service.get_evidence("E-BF")["status"], "confirmed")
        # 结论只基于已确认证据
        zones = self.service.conclusions(INCIDENT)["impact_zones"]
        self.assertEqual(len(zones), 1)
        self.assertTrue(self.service.verify_chain()["valid"])

    def test_atomic_import_rejects_whole_batch(self):
        before = self.service.verify_chain()["checked"]
        result = self.service.import_batch(INCIDENT, self.mixed_batch(), mode="atomic")
        self.assertFalse(result["committed"])
        self.assertEqual(result["imported"], 0)
        self.assertEqual(result["quarantined"], 6)
        # 整批拒绝：主日志只新增 0 条业务事件（隔离区独立记录）
        self.assertEqual(self.service.verify_chain()["checked"], before)
        with self.assertRaises(Exception):
            self.service.get_evidence("E-OK")

    def test_atomic_import_all_valid_commits(self):
        result = self.service.import_batch(INCIDENT, [valid_sensor("E-A"), valid_sensor("E-B")], mode="atomic")
        self.assertTrue(result["committed"])
        self.assertEqual(result["imported"], 2)

    def test_batch_internal_duplicate_rejected(self):
        result = self.service.import_batch(
            INCIDENT, [valid_sensor("E-TWIN"), valid_sensor("E-TWIN")], mode="partial"
        )
        self.assertEqual(result["imported"], 1)
        self.assertEqual(result["quarantined"], 1)
        self.assertIn("重复", result["rejected"][0]["errors"][0])


if __name__ == "__main__":
    unittest.main()
