import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from overflow_evidence.ledger import (
    BackfillError,
    BackfillMeta,
    DuplicateEvidenceError,
    EvidenceLedger,
    LedgerError,
    PayloadError,
    UnknownEvidenceError,
)

UTC = timezone.utc
T0 = datetime(2026, 9, 29, 8, 0, tzinfo=UTC)


def make_ledger(**kw):
    kw.setdefault("clock", lambda: T0 + timedelta(hours=10))
    lg = EvidenceLedger(**kw)
    lg.open_incident("INC-1", T0)
    return lg


def sensor_payload(value=120.0, limit=40.0):
    return {"metric": "cod", "value": value, "limit": limit}


class AppendTests(unittest.TestCase):
    def test_append_assigns_sequence_and_hash(self):
        lg = make_ledger()
        r1 = lg.append(incident_id="INC-1", kind="sensor", observed_at=T0 + timedelta(hours=1),
                       location_code="L-1", payload=sensor_payload())
        r2 = lg.append(incident_id="INC-1", kind="inspection", observed_at=T0 + timedelta(hours=2),
                       location_code="L-1", payload={"overflow_observed": True})
        self.assertEqual((r1.sequence, r2.sequence), (1, 2))
        self.assertNotEqual(r1.entry_hash, r2.entry_hash)
        self.assertTrue(lg.verify_chain())

    def test_naive_time_rejected(self):
        lg = make_ledger()
        with self.assertRaises(ValueError):
            lg.append(incident_id="INC-1", kind="sensor", observed_at=datetime(2026, 9, 29, 9),
                      location_code="L-1", payload=sensor_payload())

    def test_unknown_incident_rejected(self):
        lg = make_ledger()
        with self.assertRaises(LedgerError):
            lg.append(incident_id="NOPE", kind="sensor", observed_at=T0,
                      location_code="L-1", payload=sensor_payload())

    def test_duplicate_id_rejected(self):
        lg = make_ledger()
        kw = dict(incident_id="INC-1", kind="sensor", observed_at=T0 + timedelta(hours=1),
                  location_code="L-1", payload=sensor_payload(), evidence_id="E-1")
        lg.append(**kw)
        with self.assertRaises(DuplicateEvidenceError):
            lg.append(**kw)

    def test_payload_validation(self):
        lg = make_ledger()
        with self.assertRaises(PayloadError):
            lg.append(incident_id="INC-1", kind="sensor", observed_at=T0 + timedelta(hours=1),
                      location_code="L-1", payload={"metric": "cod", "value": "高", "limit": 40})
        with self.assertRaises(PayloadError):
            lg.append(incident_id="INC-1", kind="work_receipt", observed_at=T0 + timedelta(hours=1),
                      location_code="L-1", payload={"action": "dance", "completed": True})


class BackfillTests(unittest.TestCase):
    def test_late_record_requires_reason_and_dual_confirmation(self):
        lg = make_ledger()
        with self.assertRaises(BackfillError):
            lg.append(incident_id="INC-1", kind="sample", observed_at=T0 - timedelta(hours=3),
                      location_code="L-1",
                      payload={"analyte": "cod", "value": 90.0, "limit": 40.0})

    def test_backfill_needs_two_distinct_confirmers(self):
        lg = make_ledger()
        payload = {"analyte": "cod", "value": 90.0, "limit": 40.0}
        observed = T0 - timedelta(hours=3)
        with self.assertRaises(BackfillError):
            lg.append(incident_id="INC-1", kind="sample", observed_at=observed,
                      location_code="L-1", payload=payload,
                      backfill=BackfillMeta("补采", ("张三", "张三")))
        with self.assertRaises(BackfillError):
            lg.append(incident_id="INC-1", kind="sample", observed_at=observed,
                      location_code="L-1", payload=payload,
                      backfill=BackfillMeta("", ("张三", "李四")))
        rec = lg.append(incident_id="INC-1", kind="sample", observed_at=observed,
                        location_code="L-1", payload=payload,
                        backfill=BackfillMeta("实验室补送中秋夜样本", ("张三", "李四")))
        self.assertIsNotNone(rec.backfill)

    def test_recorded_far_after_observed_counts_as_backfill(self):
        lg = make_ledger()
        with self.assertRaises(BackfillError):
            lg.append(incident_id="INC-1", kind="sensor", observed_at=T0 + timedelta(hours=1),
                      recorded_at=T0 + timedelta(days=3),
                      location_code="L-1", payload=sensor_payload())


class WithdrawTests(unittest.TestCase):
    def test_withdraw_keeps_original_record(self):
        lg = make_ledger()
        rec = lg.append(incident_id="INC-1", kind="sensor", observed_at=T0 + timedelta(hours=1),
                        location_code="L-1", payload=sensor_payload())
        lg.withdraw(rec.evidence_id, reason="编号有误", withdrawn_by="值班长")
        self.assertEqual(len(lg.records("INC-1", include_withdrawn=True)), 1)
        self.assertEqual(len(lg.records("INC-1")), 0)
        self.assertTrue(lg.is_withdrawn(rec.evidence_id))

    def test_double_withdraw_and_unknown_rejected(self):
        lg = make_ledger()
        rec = lg.append(incident_id="INC-1", kind="sensor", observed_at=T0 + timedelta(hours=1),
                        location_code="L-1", payload=sensor_payload())
        lg.withdraw(rec.evidence_id, reason="r", withdrawn_by="u")
        with self.assertRaises(LedgerError):
            lg.withdraw(rec.evidence_id, reason="r", withdrawn_by="u")
        with self.assertRaises(UnknownEvidenceError):
            lg.withdraw("NOPE", reason="r", withdrawn_by="u")

    def test_timeline_orders_by_observed_time(self):
        lg = make_ledger()
        lg.append(incident_id="INC-1", kind="sensor", observed_at=T0 + timedelta(hours=5),
                  location_code="L-1", payload=sensor_payload())
        lg.append(incident_id="INC-1", kind="sensor", observed_at=T0 + timedelta(hours=1),
                  location_code="L-1", payload=sensor_payload())
        times = [r.observed_at for r in lg.timeline("INC-1")]
        self.assertEqual(times, sorted(times))

    def test_chain_detects_tampering(self):
        lg = make_ledger()
        lg.append(incident_id="INC-1", kind="sensor", observed_at=T0 + timedelta(hours=1),
                  location_code="L-1", payload=sensor_payload())
        lg._records[0].payload["value"] = 1.0  # 直接篡改内部状态
        self.assertFalse(lg.verify_chain())

    def test_dump_load_roundtrip(self):
        lg = make_ledger()
        lg.append(incident_id="INC-1", kind="sensor", observed_at=T0 + timedelta(hours=1),
                  location_code="L-1", payload=sensor_payload())
        lg2 = EvidenceLedger()
        lg2.load(lg.dump())
        self.assertTrue(lg2.verify_chain())
        self.assertEqual(lg2.records("INC-1")[0].payload["value"], 120.0)


if __name__ == "__main__":
    unittest.main()
