import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from overflow_evidence.service import IncidentService

UTC = timezone.utc
T0 = datetime(2026, 9, 29, 8, 0, tzinfo=UTC)


def make_service():
    clock = [T0 + timedelta(hours=10)]
    svc = IncidentService(clock=lambda: clock[0])
    svc.create_incident("INC-1", T0, "中秋冒溢")
    svc.submit_evidence("INC-1", kind="sensor", observed_at=T0 + timedelta(hours=1),
                        location_code="BLOCK-2",
                        payload={"metric": "cod", "value": 120.0, "limit": 40.0})
    svc.submit_evidence("INC-1", kind="inspection", observed_at=T0 + timedelta(hours=1, minutes=30),
                        location_code="BLOCK-2",
                        payload={"overflow_observed": True, "notes": "井口冒溢"})
    svc.submit_evidence("INC-1", kind="work_receipt", observed_at=T0 + timedelta(hours=2),
                        location_code="BLOCK-2",
                        payload={"action": "sealing", "completed": True})
    return svc, clock


class ReportTests(unittest.TestCase):
    def test_report_has_verifiable_digest(self):
        svc, _ = make_service()
        r = svc.generate_report("INC-1")
        v = svc.verify_report(r.report_id)
        self.assertTrue(v["valid"])
        self.assertEqual(v["stored_digest"], v["recomputed_digest"])

    def test_tampered_snapshot_fails_verification(self):
        svc, _ = make_service()
        r = svc.generate_report("INC-1")
        r.snapshot["segments"]["BLOCK-2"]["location_code"] = "BLOCK-9"
        self.assertFalse(svc.verify_report(r.report_id)["valid"])

    def test_withdraw_recomputes_but_keeps_old_report(self):
        svc, _ = make_service()
        r1 = svc.generate_report("INC-1")
        svc.withdraw_evidence("EV-00001", reason="传感器漂移", withdrawn_by="值班长")
        svc.withdraw_evidence("EV-00002", reason="照片编号与现场不符", withdrawn_by="值班长")
        r2 = svc.generate_report("INC-1")
        self.assertEqual((r1.version, r2.version), (1, 2))
        # 旧报告原样保留
        self.assertIn("BLOCK-2", svc.get_report(r1.report_id).snapshot["segments"])
        # 新报告中超标证据均被撤回，区段消失
        self.assertNotIn("BLOCK-2", r2.snapshot["segments"])
        self.assertEqual(r2.snapshot["evidence"]["withdrawn"], ["EV-00001", "EV-00002"])

    def test_diff_reports(self):
        svc, _ = make_service()
        r1 = svc.generate_report("INC-1")
        svc.withdraw_evidence("EV-00001", reason="传感器漂移", withdrawn_by="值班长")
        svc.withdraw_evidence("EV-00002", reason="照片编号与现场不符", withdrawn_by="值班长")
        r2 = svc.generate_report("INC-1")
        diff = svc.diff_reports(r1.report_id, r2.report_id)
        self.assertEqual(diff["evidence"]["withdrawn"], ["EV-00001", "EV-00002"])
        self.assertEqual(diff["segments"]["removed"], ["BLOCK-2"])
        self.assertEqual(diff["remediations"]["removed"],
                         ["clean:BLOCK-2", "contain:BLOCK-2", "restore:BLOCK-2", "verify:BLOCK-2"])

    def test_report_ids_are_immutable(self):
        svc, _ = make_service()
        r1 = svc.generate_report("INC-1")
        r2 = svc.generate_report("INC-1")
        self.assertNotEqual(r1.report_id, r2.report_id)
        self.assertEqual(len(svc.list_reports("INC-1")), 2)


class ConclusionEvidenceTests(unittest.TestCase):
    def test_segment_and_stage_refs(self):
        svc, _ = make_service()
        r = svc.generate_report("INC-1")
        seg_ev = svc.conclusion_evidence(r.report_id, "segment:BLOCK-2")
        self.assertEqual(len(seg_ev), 3)
        stage_ev = svc.conclusion_evidence(r.report_id, "stage:BLOCK-2:containment")
        self.assertEqual(len(stage_ev), 1)
        self.assertEqual(stage_ev[0]["payload"]["action"], "sealing")
        rem_ev = svc.conclusion_evidence(r.report_id, "remediation:contain:BLOCK-2")
        self.assertEqual(rem_ev[0]["evidence_id"], stage_ev[0]["evidence_id"])

    def test_unknown_ref_rejected(self):
        svc, _ = make_service()
        r = svc.generate_report("INC-1")
        with self.assertRaises(KeyError):
            svc.conclusion_evidence(r.report_id, "segment:NOWHERE")
        with self.assertRaises(KeyError):
            svc.conclusion_evidence(r.report_id, "bogus")


class PersistenceTests(unittest.TestCase):
    def test_save_load_roundtrip(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "db.json"
            svc, _ = make_service()
            svc._store_path = path
            svc.generate_report("INC-1")
            svc2 = IncidentService(store_path=path)
            self.assertEqual(len(svc2.list_reports("INC-1")), 1)
            self.assertEqual(len(svc2.timeline("INC-1")), 3)
            self.assertTrue(svc2.verify_chain())
            self.assertEqual(len(svc2.remediations_for("INC-1")), 4)


if __name__ == "__main__":
    unittest.main()
