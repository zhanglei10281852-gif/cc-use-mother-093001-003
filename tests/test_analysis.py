import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from overflow_evidence.analysis import derive_conclusions
from overflow_evidence.ledger import EvidenceLedger

UTC = timezone.utc
T0 = datetime(2026, 9, 29, 8, 0, tzinfo=UTC)


def build_ledger():
    lg = EvidenceLedger(clock=lambda: T0 + timedelta(hours=10))
    lg.open_incident("INC-1", T0)
    return lg


def add(lg, kind, hours, loc, payload):
    return lg.append(incident_id="INC-1", kind=kind, observed_at=T0 + timedelta(hours=hours),
                     location_code=loc, payload=payload)


class SegmentTests(unittest.TestCase):
    def test_exceedance_creates_affected_segment(self):
        lg = build_ledger()
        add(lg, "sensor", 1, "BLOCK-2", {"metric": "cod", "value": 120.0, "limit": 40.0})
        add(lg, "sensor", 2, "BLOCK-5", {"metric": "cod", "value": 10.0, "limit": 40.0})
        concl = derive_conclusions(lg.records("INC-1"))
        self.assertEqual([s.location_code for s in concl.segments], ["BLOCK-2"])

    def test_inspection_overflow_counts_as_exceedance(self):
        lg = build_ledger()
        add(lg, "inspection", 1, "BLOCK-3", {"overflow_observed": True})
        concl = derive_conclusions(lg.records("INC-1"))
        self.assertEqual(len(concl.segments), 1)
        self.assertEqual(concl.segments[0].current_stage, "detection")


class StageTests(unittest.TestCase):
    def test_stages_progress_with_work_receipts(self):
        lg = build_ledger()
        add(lg, "sensor", 1, "L", {"metric": "cod", "value": 120.0, "limit": 40.0})
        add(lg, "work_receipt", 2, "L", {"action": "sealing", "completed": True})
        add(lg, "work_receipt", 3, "L", {"action": "pumping", "completed": True})
        add(lg, "work_receipt", 4, "L", {"action": "repair", "completed": True})
        add(lg, "sample", 6, "L", {"analyte": "cod", "value": 20.0, "limit": 40.0})
        seg = derive_conclusions(lg.records("INC-1")).segments[0]
        self.assertEqual(seg.current_stage, "verification")
        for s in seg.stages:
            self.assertTrue(s.reached, s.stage)

    def test_receipt_before_detection_does_not_count(self):
        lg = build_ledger()
        add(lg, "work_receipt", 0.5, "L", {"action": "sealing", "completed": True})
        add(lg, "sensor", 1, "L", {"metric": "cod", "value": 120.0, "limit": 40.0})
        seg = derive_conclusions(lg.records("INC-1")).segments[0]
        self.assertFalse(seg.stage("containment").reached)

    def test_uncompleted_receipt_does_not_count(self):
        lg = build_ledger()
        add(lg, "sensor", 1, "L", {"metric": "cod", "value": 120.0, "limit": 40.0})
        add(lg, "work_receipt", 2, "L", {"action": "sealing", "completed": False})
        seg = derive_conclusions(lg.records("INC-1")).segments[0]
        self.assertFalse(seg.stage("containment").reached)

    def test_verification_requires_clearance_after_last_exceedance(self):
        lg = build_ledger()
        add(lg, "sample", 1, "L", {"analyte": "cod", "value": 20.0, "limit": 40.0})  # 合格但太早
        add(lg, "sensor", 2, "L", {"metric": "cod", "value": 120.0, "limit": 40.0})
        seg = derive_conclusions(lg.records("INC-1")).segments[0]
        self.assertFalse(seg.stage("verification").reached)


class RemediationDerivationTests(unittest.TestCase):
    def test_items_have_dependencies_and_due_times(self):
        lg = build_ledger()
        add(lg, "sensor", 1, "BLOCK-2", {"metric": "cod", "value": 120.0, "limit": 40.0})
        concl = derive_conclusions(lg.records("INC-1"))
        by_key = {r.key: r for r in concl.remediations}
        self.assertEqual(set(by_key), {"contain:BLOCK-2", "clean:BLOCK-2",
                                       "restore:BLOCK-2", "verify:BLOCK-2"})
        self.assertEqual(by_key["clean:BLOCK-2"].depends_on, ["contain:BLOCK-2"])
        self.assertEqual(by_key["verify:BLOCK-2"].depends_on, ["restore:BLOCK-2"])
        first = T0 + timedelta(hours=1)
        self.assertEqual(by_key["contain:BLOCK-2"].due_at, first + timedelta(hours=4))
        self.assertEqual(by_key["verify:BLOCK-2"].due_at, first + timedelta(hours=72))
        self.assertFalse(by_key["contain:BLOCK-2"].satisfied)

    def test_satisfied_follows_stage_evidence(self):
        lg = build_ledger()
        add(lg, "sensor", 1, "L", {"metric": "cod", "value": 120.0, "limit": 40.0})
        receipt = add(lg, "work_receipt", 2, "L", {"action": "sealing", "completed": True})
        concl = derive_conclusions(lg.records("INC-1"))
        contain = next(r for r in concl.remediations if r.key == "contain:L")
        self.assertTrue(contain.satisfied)
        self.assertEqual(contain.evidence_ids, [receipt.evidence_id])


if __name__ == "__main__":
    unittest.main()
