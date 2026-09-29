import json, sys
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent / "src"))
from overflow_evidence.contracts import EvidenceItem, EvidenceKind

item = EvidenceItem("E-9", EvidenceKind.SAMPLE, datetime(2026, 9, 29, 8, tzinfo=timezone.utc), "BLOCK-2")
print(json.dumps({"evidence_id": item.evidence_id, "kind": item.kind.value, "location": item.location_code}, ensure_ascii=False))
