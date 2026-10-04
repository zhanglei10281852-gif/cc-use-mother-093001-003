"""批量导入：逐条校验，坏记录进隔离区，不污染已确认数据。

导入不是事务性的"全有或全无"：每条记录独立校验、独立入账，
非法记录（缺字段、时间无时区、内容不合规、补录缺确认、编号重复等）
进入隔离区并附带错误原因，已入账的有效记录不受任何影响。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .contracts import EvidenceKind
from .ledger import BackfillMeta, EvidenceLedger, EvidenceRecord, LedgerError


@dataclass
class QuarantineEntry:
    index: int
    record: Any
    errors: list[str]

    def to_dict(self) -> dict:
        return {"index": self.index, "record": self.record, "errors": list(self.errors)}


@dataclass
class ImportResult:
    accepted: list[EvidenceRecord] = field(default_factory=list)
    quarantine: list[QuarantineEntry] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "accepted_count": len(self.accepted),
            "rejected_count": len(self.quarantine),
            "accepted_ids": [r.evidence_id for r in self.accepted],
            "quarantine": [q.to_dict() for q in self.quarantine],
        }


def _prepare(raw: Any) -> dict:
    """把一条原始记录规整为 append 所需参数，非法即抛错。"""
    if not isinstance(raw, dict):
        raise ValueError("记录必须是对象")
    for name in ("kind", "observed_at", "location_code", "payload"):
        if name not in raw:
            raise ValueError(f"缺少字段 {name}")
    kind = EvidenceKind(raw["kind"])  # 非法取值抛 ValueError
    backfill = None
    bf = raw.get("backfill")
    if bf is not None:
        if not isinstance(bf, dict):
            raise ValueError("backfill 必须是对象")
        backfill = BackfillMeta(reason=str(bf.get("reason", "")),
                                confirmed_by=tuple(bf.get("confirmed_by") or ()))
    return {
        "kind": kind,
        "observed_at": raw["observed_at"],
        "location_code": raw["location_code"],
        "payload": raw["payload"],
        "evidence_id": raw.get("evidence_id"),
        "source": raw.get("source", "batch"),
        "recorded_at": raw.get("recorded_at"),
        "backfill": backfill,
    }


def import_batch(
    ledger: EvidenceLedger,
    incident_id: str,
    records: list[Any],
    *,
    source: str = "batch",
) -> ImportResult:
    """逐条导入；每条独立成败，坏记录进入隔离区。"""
    result = ImportResult()
    for i, raw in enumerate(records):
        try:
            kwargs = _prepare(raw)
            kwargs.setdefault("source", source)
            if kwargs["source"] is None:
                kwargs["source"] = source
            rec = ledger.append(incident_id=incident_id, **kwargs)
            result.accepted.append(rec)
        except (LedgerError, ValueError, TypeError, KeyError) as exc:
            result.quarantine.append(QuarantineEntry(index=i, record=raw, errors=[str(exc)]))
    return result
