"""溢流证据和整改工作的基础契约。"""
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class EvidenceKind(StrEnum):
    SENSOR = "sensor"
    INSPECTION = "inspection"
    SAMPLE = "sample"
    WORK_RECEIPT = "work_receipt"


@dataclass(frozen=True)
class EvidenceItem:
    evidence_id: str
    kind: EvidenceKind
    observed_at: datetime
    location_code: str

    def __post_init__(self) -> None:
        if self.observed_at.tzinfo is None:
            raise ValueError("证据时间必须包含时区")


@dataclass(frozen=True)
class RemediationItem:
    item_id: str
    incident_id: str
    depends_on: tuple[str, ...] = ()
