import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from overflow_evidence.analysis import derive_conclusions
from overflow_evidence.ledger import EvidenceLedger
from overflow_evidence.remediation import RemediationError, RemediationRegistry, TaskStatus

UTC = timezone.utc
T0 = datetime(2026, 9, 29, 8, 0, tzinfo=UTC)


def make_registry(with_receipts=True):
    lg = EvidenceLedger(clock=lambda: T0 + timedelta(hours=10))
    lg.open_incident("INC-1", T0)

    def add(kind, hours, payload, loc="L"):
        return lg.append(incident_id="INC-1", kind=kind,
                         observed_at=T0 + timedelta(hours=hours),
                         location_code=loc, payload=payload)

    add("sensor", 1, {"metric": "cod", "value": 120.0, "limit": 40.0})
    if with_receipts:
        add("work_receipt", 2, {"action": "sealing", "completed": True})
    reg = RemediationRegistry()
    reg.sync("INC-1", derive_conclusions(lg.records("INC-1")).remediations)
    return lg, reg


class SyncTests(unittest.TestCase):
    def test_sync_creates_tasks_and_marks_satisfied(self):
        _, reg = make_registry()
        contain = reg.get("INC-1:contain:L")
        clean = reg.get("INC-1:clean:L")
        self.assertEqual(contain.status, TaskStatus.SATISFIED)
        self.assertEqual(clean.status, TaskStatus.OPEN)
        self.assertEqual(clean.depends_on, ["INC-1:contain:L"])

    def test_withdrawn_evidence_reverts_satisfaction(self):
        lg, reg = make_registry()
        receipt = next(r for r in lg.records("INC-1") if r.kind == "work_receipt")
        lg.withdraw(receipt.evidence_id, reason="回执造假", withdrawn_by="复核员")
        reg.sync("INC-1", derive_conclusions(lg.records("INC-1")).remediations)
        contain = reg.get("INC-1:contain:L")
        self.assertFalse(contain.satisfied)
        self.assertEqual(contain.status, TaskStatus.OPEN)


class ReinspectionTests(unittest.TestCase):
    def test_pass_requires_satisfied_and_verified_dependencies(self):
        lg, reg = make_registry()
        # 依赖未复验，不能通过 clean
        with self.assertRaises(RemediationError):
            reg.record_reinspection("INC-1:clean:L", evidence_id="EV-00001",
                                    passed=True, inspector="王工")
        # contain 未满足条件时也不能通过（先撤回回执）
        receipt = next(r for r in lg.records("INC-1") if r.kind == "work_receipt")
        lg.withdraw(receipt.evidence_id, reason="r", withdrawn_by="u")
        reg.sync("INC-1", derive_conclusions(lg.records("INC-1")).remediations)
        with self.assertRaises(RemediationError):
            reg.record_reinspection("INC-1:contain:L", evidence_id="EV-00001",
                                    passed=True, inspector="王工")

    def test_full_chain_verifies_in_order(self):
        lg = EvidenceLedger(clock=lambda: T0 + timedelta(hours=10))
        lg.open_incident("INC-1", T0)

        def add(kind, hours, payload):
            return lg.append(incident_id="INC-1", kind=kind,
                             observed_at=T0 + timedelta(hours=hours),
                             location_code="L", payload=payload)

        add("sensor", 1, {"metric": "cod", "value": 120.0, "limit": 40.0})
        add("work_receipt", 2, {"action": "sealing", "completed": True})
        add("work_receipt", 3, {"action": "cleaning", "completed": True})
        add("work_receipt", 4, {"action": "repair", "completed": True})
        clear = add("sample", 6, {"analyte": "cod", "value": 20.0, "limit": 40.0})

        reg = RemediationRegistry()
        reg.sync("INC-1", derive_conclusions(lg.records("INC-1")).remediations)
        for key in ("contain", "clean", "restore", "verify"):
            reg.record_reinspection(f"INC-1:{key}:L", evidence_id=clear.evidence_id,
                                    passed=True, inspector="王工")
        self.assertTrue(all(t.status is TaskStatus.VERIFIED for t in reg.for_incident("INC-1")))

    def test_failed_reinspection_reopens(self):
        _, reg = make_registry()
        reg.record_reinspection("INC-1:contain:L", evidence_id="EV-00001",
                                passed=False, inspector="王工", note="封控范围不足")
        t = reg.get("INC-1:contain:L")
        self.assertEqual(t.status, TaskStatus.OPEN)
        self.assertEqual(len(t.reinspections), 1)


class EscalationTests(unittest.TestCase):
    def test_overdue_task_escalates_once(self):
        _, reg = make_registry()
        out = reg.check_overdue(T0 + timedelta(hours=100))
        escalated = {e["task_id"] for e in out}
        # contain 已满足但未复验，其余未完成，全部逾期 → 均升级
        self.assertEqual(len(out), 4)
        self.assertIn("INC-1:verify:L", escalated)
        again = reg.check_overdue(T0 + timedelta(hours=101))
        self.assertEqual(again, [])  # 已升级的不能重复升级
        self.assertEqual(reg.get("INC-1:clean:L").status, TaskStatus.ESCALATED)

    def test_verified_task_never_escalates(self):
        _, reg = make_registry()
        reg.record_reinspection("INC-1:contain:L", evidence_id="EV-00001",
                                passed=True, inspector="王工")
        out = reg.check_overdue(T0 + timedelta(hours=100))
        self.assertNotIn("INC-1:contain:L", {e["task_id"] for e in out})


class CompletionViewTests(unittest.TestCase):
    def test_completion_reports_dependencies_and_closable(self):
        _, reg = make_registry()
        view = reg.completion("INC-1:clean:L", T0 + timedelta(hours=3))
        self.assertFalse(view["dependencies_met"])
        self.assertFalse(view["closable"])
        reg.record_reinspection("INC-1:contain:L", evidence_id="EV-00001",
                                passed=True, inspector="王工")
        view = reg.completion("INC-1:contain:L", T0 + timedelta(hours=3))
        self.assertEqual(view["status"], "verified")
        self.assertFalse(view["overdue"])


if __name__ == "__main__":
    unittest.main()
