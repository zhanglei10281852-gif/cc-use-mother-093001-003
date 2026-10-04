import json
import tempfile
import unittest
from pathlib import Path

from helpers import INCIDENT, build_midautumn_scenario, make_service
from overflow_evidence.errors import NotFoundError, ValidationError


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = make_service(self.tmp.name)
        build_midautumn_scenario(self.service)

    def test_issue_and_verify_report(self):
        report = self.service.issue_report(INCIDENT, "复盘组")
        self.assertEqual(report["version"], 1)
        self.assertEqual(report["report_id"], f"RPT-{INCIDENT}-001")
        self.assertEqual(len(report["digest"]), 64)
        self.assertEqual(report["summary"]["evidence_count"], 7)
        self.assertEqual(report["summary"]["zones_cleared"], 1)
        result = self.service.verify_report(report["report_id"])
        self.assertTrue(result["valid"], result["checks"])

    def test_withdrawal_keeps_old_report_and_recomputes(self):
        v1 = self.service.issue_report(INCIDENT, "复盘组")
        self.service.withdraw_evidence("E-L1", "实验室复核判定样本交叉污染", "复盘组")
        v2 = self.service.issue_report(INCIDENT, "复盘组")
        # 版本与哈希链
        self.assertEqual(v2["version"], 2)
        self.assertEqual(v2["prev_digest"], v1["digest"])
        # 旧报告原样保留且仍可校验
        reloaded = self.service.get_report(v1["report_id"])
        self.assertEqual(reloaded["digest"], v1["digest"])
        self.assertTrue(self.service.verify_report(v1["report_id"])["valid"])
        # 新报告反映撤回后的结论
        self.assertEqual(v2["impact_zones"][0]["severity"], "moderate")
        excluded = {e["evidence_id"]: e for e in v2["excluded_evidence"]}
        self.assertEqual(excluded["E-L1"]["status"], "withdrawn")
        # 差异可查
        diff = self.service.diff_reports(INCIDENT, v1["report_id"], v2["report_id"])
        self.assertEqual(diff["evidence"]["removed"], ["E-L1"])
        zone_change = diff["impact_zones"]["changed"][0]["changes"]
        self.assertEqual(zone_change["severity"], {"from": "severe", "to": "moderate"})
        self.assertIn(f"REM-{INCIDENT}-BLOCK-2-REPAIR", diff["remediation"]["removed"])

    def test_pending_backfill_listed_as_excluded(self):
        self.service.register_evidence(
            INCIDENT, kind="inspection", observed_at="2026-09-27T08:00:00+00:00",
            location_code="BLOCK-2", payload={"overflow_observed": True},
            source="环卫", registered_by="甲", backfill_reason="补录",
        )
        report = self.service.issue_report(INCIDENT, "复盘组")
        excluded = {e["evidence_id"]: e for e in report["excluded_evidence"]}
        pending = [e for e in excluded.values() if e["status"] == "pending"]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["reason"], "awaiting_dual_confirmation")
        self.assertEqual(report["summary"]["evidence_count"], 7)  # 待确认不计入

    def test_tampered_report_fails_verification(self):
        report = self.service.issue_report(INCIDENT, "复盘组")
        path = Path(self.tmp.name) / "reports" / f"{report['report_id']}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["impact_zones"][0]["severity"] = "minor"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        result = self.service.verify_report(report["report_id"])
        self.assertFalse(result["valid"])
        digest_check = next(c for c in result["checks"] if c["name"] == "digest_self_consistent")
        self.assertFalse(digest_check["ok"])

    def test_provenance_traces_conclusion_to_materials(self):
        report = self.service.issue_report(INCIDENT, "复盘组")
        zone = self.service.conclusion_evidence(report["report_id"], "zone:ZONE-BLOCK-2")
        self.assertEqual(
            [m["evidence_id"] for m in zone["materials"]],
            ["E-S1", "E-I1", "E-L1", "E-W1", "E-W2", "E-W3", "E-I2"],
        )
        self.assertTrue(all(m["content_hash"] for m in zone["materials"]))
        phase = self.service.conclusion_evidence(report["report_id"], "phase:detection")
        self.assertEqual([m["evidence_id"] for m in phase["materials"]], ["E-S1"])
        item = self.service.conclusion_evidence(
            report["report_id"], f"remediation:REM-{INCIDENT}-BLOCK-2-CLEAN"
        )
        self.assertEqual([m["evidence_id"] for m in item["materials"]], ["E-W2"])
        with self.assertRaises(NotFoundError):
            self.service.conclusion_evidence(report["report_id"], "zone:ZONE-NOPE")
        with self.assertRaises(ValidationError):
            self.service.conclusion_evidence(report["report_id"], "bogus")

    def test_diff_rejects_cross_incident(self):
        self.service.create_incident("INC-OTHER", "另一事件", actor="管理方")
        other = self.service.issue_report("INC-OTHER", "复盘组")
        mine = self.service.issue_report(INCIDENT, "复盘组")
        with self.assertRaises(ValidationError):
            self.service.diff_reports(INCIDENT, mine["report_id"], other["report_id"])

    def test_report_files_are_immutable(self):
        report = self.service.issue_report(INCIDENT, "复盘组")
        with self.assertRaises(FileExistsError):
            self.service.store.save_report(report)


if __name__ == "__main__":
    unittest.main()
