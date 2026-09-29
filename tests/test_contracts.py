import sys, unittest
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
from overflow_evidence.contracts import EvidenceItem, EvidenceKind, RemediationItem


class EvidenceContractTests(unittest.TestCase):
    def test_evidence_keeps_event_time(self):
        now = datetime.now(timezone.utc)
        item = EvidenceItem("E-1", EvidenceKind.SENSOR, now, "L-1")
        self.assertEqual(item.observed_at, now)

    def test_dependencies_are_immutable(self):
        item = RemediationItem("R-1", "I-1", ("R-0",))
        self.assertEqual(item.depends_on, ("R-0",))


if __name__ == "__main__": unittest.main()
