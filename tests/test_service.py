import tempfile
import unittest
from datetime import datetime, timezone

from helpers import INCIDENT, Clock, build_midautumn_scenario, make_service
from overflow_evidence.errors import ConflictError, NotFoundError, ValidationError


class EvidenceLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock()
        self.service = make_service(self.tmp.name, self.clock)
        self.service.create_incident(INCIDENT, "中秋冒溢", actor="管理方")

    def test_timely_evidence_is_confirmed_immediately(self):
        record = self.service.register_evidence(
            INCIDENT, kind="sensor", observed_at="2026-09-29T08:00:00+00:00",
            location_code="BLOCK-2", payload={"parameter": "cod", "value": 45.0, "threshold": 30.0},
            source="排水公司", registered_by="平台",
        )
        self.assertEqual(record["status"], "confirmed")
        self.assertFalse(record["is_backfill"])

    def test_backfill_requires_reason(self):
        with self.assertRaises(ValidationError):
            self.service.register_evidence(
                INCIDENT, kind="inspection", observed_at="2026-09-27T08:00:00+00:00",
                location_code="BLOCK-2", payload={"overflow_observed": True},
                source="环卫", registered_by="甲",
            )

    def test_backfill_needs_dual_confirmation(self):
        record = self.service.register_evidence(
            INCIDENT, kind="inspection", observed_at="2026-09-27T08:00:00+00:00",
            location_code="BLOCK-2", payload={"overflow_observed": True},
            source="环卫", registered_by="甲", backfill_reason="现场纸质记录后补",
        )
        self.assertEqual(record["status"], "pending")
        self.assertTrue(record["is_backfill"])
        # 待确认证据不参与结论
        self.assertEqual(self.service.conclusions(INCIDENT)["impact_zones"], [])
        # 登记人不能确认自己的补录
        with self.assertRaises(ValidationError):
            self.service.confirm_evidence(record["evidence_id"], "甲")
        # 第一人确认后仍为待确认
        self.service.confirm_evidence(record["evidence_id"], "乙")
        self.assertEqual(self.service.get_evidence(record["evidence_id"])["status"], "pending")
        # 同一人不能重复确认
        with self.assertRaises(ConflictError):
            self.service.confirm_evidence(record["evidence_id"], "乙")
        # 第二名确认人确认后生效
        confirmed = self.service.confirm_evidence(record["evidence_id"], "丙")
        self.assertEqual(confirmed["status"], "confirmed")
        zones = self.service.conclusions(INCIDENT)["impact_zones"]
        self.assertEqual(len(zones), 1)
        # 已确认后不能再走确认流程
        with self.assertRaises(ConflictError):
            self.service.confirm_evidence(record["evidence_id"], "丁")

    def test_explicit_backfill_flag(self):
        record = self.service.register_evidence(
            INCIDENT, kind="inspection", observed_at="2026-09-29T10:00:00+00:00",
            location_code="BLOCK-2", payload={"overflow_observed": False},
            source="环卫", registered_by="甲", is_backfill=True, backfill_reason="补登巡查",
        )
        self.assertEqual(record["status"], "pending")

    def test_future_observed_at_rejected(self):
        with self.assertRaises(ValidationError):
            self.service.register_evidence(
                INCIDENT, kind="sensor", observed_at="2026-09-30T08:00:00+00:00",
                location_code="BLOCK-2", payload={"parameter": "cod", "value": 1.0, "threshold": 30.0},
                source="排水公司", registered_by="平台",
            )

    def test_naive_timestamp_rejected(self):
        with self.assertRaises(ValidationError):
            self.service.register_evidence(
                INCIDENT, kind="sensor", observed_at="2026-09-29T08:00:00",
                location_code="BLOCK-2", payload={"parameter": "cod", "value": 1.0, "threshold": 30.0},
                source="排水公司", registered_by="平台",
            )

    def test_duplicate_evidence_id_rejected(self):
        kwargs = dict(
            kind="sensor", observed_at="2026-09-29T08:00:00+00:00", location_code="BLOCK-2",
            payload={"parameter": "cod", "value": 45.0, "threshold": 30.0},
            source="排水公司", registered_by="平台", evidence_id="E-DUP",
        )
        self.service.register_evidence(INCIDENT, **kwargs)
        with self.assertRaises(ConflictError):
            self.service.register_evidence(INCIDENT, **kwargs)

    def test_withdraw_requires_reason_and_is_terminal(self):
        record = self.service.register_evidence(
            INCIDENT, kind="sensor", observed_at="2026-09-29T08:00:00+00:00",
            location_code="BLOCK-2", payload={"parameter": "cod", "value": 45.0, "threshold": 30.0},
            source="排水公司", registered_by="平台",
        )
        with self.assertRaises(ValidationError):
            self.service.withdraw_evidence(record["evidence_id"], "", "复盘组")
        withdrawn = self.service.withdraw_evidence(record["evidence_id"], "仪表故障误报", "复盘组")
        self.assertEqual(withdrawn["status"], "withdrawn")
        self.assertEqual(withdrawn["withdrawn"]["reason"], "仪表故障误报")
        with self.assertRaises(ConflictError):
            self.service.withdraw_evidence(record["evidence_id"], "重复撤回", "复盘组")
        # 撤回后证据仍在案（只追加不删除）
        self.assertEqual(self.service.get_evidence(record["evidence_id"])["evidence_id"], record["evidence_id"])

    def test_unknown_evidence_raises_not_found(self):
        with self.assertRaises(NotFoundError):
            self.service.get_evidence("E-NOPE")


class DerivationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock()
        self.service = make_service(self.tmp.name, self.clock)
        self.ids = build_midautumn_scenario(self.service)

    def test_impact_zone_severity_and_clearing(self):
        zones = self.service.conclusions(INCIDENT)["impact_zones"]
        self.assertEqual(len(zones), 1)
        zone = zones[0]
        self.assertEqual(zone["zone_id"], "ZONE-BLOCK-2")
        self.assertEqual(zone["severity"], "severe")  # 1.5 + 1.0 + 3.0 = 5.5
        self.assertEqual(zone["status"], "cleared")
        self.assertEqual(zone["first_detected_at"], "2026-09-29T08:00:00+00:00")
        self.assertEqual(zone["cleared_at"], "2026-09-29T11:30:00+00:00")
        self.assertEqual(zone["positive_evidence_ids"], ["E-S1", "E-I1", "E-L1"])

    def test_disposal_phases_follow_evidence(self):
        phases = {p["name"]: p for p in self.service.conclusions(INCIDENT)["phases"]}
        self.assertEqual(phases["detection"]["status"], "completed")
        self.assertEqual(phases["detection"]["started_at"], "2026-09-29T08:00:00+00:00")
        self.assertEqual(phases["containment"]["started_at"], "2026-09-29T09:30:00+00:00")
        self.assertEqual(phases["remediation"]["started_at"], "2026-09-29T10:00:00+00:00")
        self.assertEqual(phases["verification"]["status"], "completed")

    def test_remediation_items_dependencies_and_completion(self):
        items = {i["item_id"]: i for i in self.service.remediation(INCIDENT)}
        clean = items[f"REM-{INCIDENT}-BLOCK-2-CLEAN"]
        repair = items[f"REM-{INCIDENT}-BLOCK-2-REPAIR"]
        verify = items[f"REM-{INCIDENT}-BLOCK-2-VERIFY"]
        self.assertEqual(clean["status"], "completed")
        self.assertEqual(clean["completion_conditions"]["type"], "work_receipt")
        self.assertEqual(repair["depends_on"], [clean["item_id"]])
        self.assertEqual(repair["status"], "completed")
        self.assertEqual(verify["depends_on"], [clean["item_id"], repair["item_id"]])
        self.assertEqual(verify["status"], "in_progress")  # 尚无复验通过记录
        self.assertEqual(verify["completion_conditions"]["type"], "passing_reinspection")

    def test_reinspection_completes_verify_item(self):
        verify_id = f"REM-{INCIDENT}-BLOCK-2-VERIFY"
        failed = self.service.record_reinspection(INCIDENT, verify_id, result="fail", inspector="复盘组", note="仍有异味")
        self.assertEqual(failed["status"], "in_progress")
        self.assertEqual(len(failed["reinspection_history"]), 1)
        passed = self.service.record_reinspection(
            INCIDENT, verify_id, result="pass", inspector="复盘组", evidence_id="E-I2"
        )
        self.assertEqual(passed["status"], "completed")
        self.assertEqual(passed["completed_at"], "2026-09-29T12:00:00+00:00")

    def test_reinspection_rejects_unknown_item_and_unconfirmed_evidence(self):
        with self.assertRaises(NotFoundError):
            self.service.record_reinspection(INCIDENT, "REM-NOPE", result="pass", inspector="复盘组")
        pending = self.service.register_evidence(
            INCIDENT, kind="inspection", observed_at="2026-09-27T08:00:00+00:00",
            location_code="BLOCK-2", payload={"overflow_observed": False},
            source="环卫", registered_by="甲", backfill_reason="补录",
        )
        with self.assertRaises(ValidationError):
            self.service.record_reinspection(
                INCIDENT, f"REM-{INCIDENT}-BLOCK-2-VERIFY", result="pass",
                inspector="复盘组", evidence_id=pending["evidence_id"],
            )

    def test_overdue_escalation_levels(self):
        # 只留污染证据、不做任何处置，时钟拨到 13 天后
        tmp2 = tempfile.TemporaryDirectory()
        self.addCleanup(tmp2.cleanup)
        clock = Clock(datetime(2026, 9, 29, 9, 0, tzinfo=timezone.utc))
        service = make_service(tmp2.name, clock)
        service.create_incident("INC-LATE", "无人处置", actor="管理方")
        service.register_evidence(
            "INC-LATE", kind="sample", observed_at="2026-09-29T08:00:00+00:00",
            location_code="BLOCK-9", payload={"parameter": "cod", "value": 120.0, "limit": 30.0},
            source="排水公司", registered_by="化验员",
        )
        service.register_evidence(
            "INC-LATE", kind="inspection", observed_at="2026-09-29T08:30:00+00:00",
            location_code="BLOCK-9", payload={"overflow_observed": True},
            source="环卫", registered_by="巡查员",
        )
        clock.now = datetime(2026, 10, 12, 8, 0, tzinfo=timezone.utc)
        escalations = {e["item_id"]: e for e in service.escalations("INC-LATE")}
        clean = escalations[f"REM-INC-LATE-BLOCK-9-CLEAN"]
        self.assertEqual(clean["escalation_level"], 3)  # 逾期 10 天 → 挂牌
        self.assertEqual(clean["escalation_label"], "挂牌")
        repair = escalations[f"REM-INC-LATE-BLOCK-9-REPAIR"]
        self.assertEqual(repair["escalation_level"], 2)  # 逾期 6 天 → 督办
        # 复验项期限 14 天，尚未逾期
        self.assertNotIn(f"REM-INC-LATE-BLOCK-9-VERIFY", escalations)

    def test_withdrawal_recomputes_conclusions(self):
        self.service.withdraw_evidence("E-L1", "实验室复核判定污染交叉", "复盘组")
        zones = self.service.conclusions(INCIDENT)["impact_zones"]
        self.assertEqual(zones[0]["severity"], "moderate")  # 1.5 + 1.0 = 2.5
        item_ids = {i["item_id"] for i in self.service.remediation(INCIDENT)}
        self.assertNotIn(f"REM-{INCIDENT}-BLOCK-2-REPAIR", item_ids)  # 非 severe 不再生成检修项

    def test_timeline_exposes_hash_chain(self):
        timeline = self.service.timeline(INCIDENT)
        self.assertGreaterEqual(len(timeline["events"]), 8)
        seqs = [e["seq"] for e in timeline["events"]]
        self.assertEqual(seqs, sorted(seqs))
        for prev, cur in zip(timeline["events"], timeline["events"][1:]):
            self.assertEqual(cur["prev_hash"], prev["hash"])


if __name__ == "__main__":
    unittest.main()
