"""事件回放得到的当前状态模型。

状态不直接落库，而是由事件日志回放重建；撤回、确认等操作只是追加新
事件，历史结论因此可以随时按任一事件子集重新计算。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .store import parse_ts

STATUS_PENDING = "pending"        # 补录待双人确认
STATUS_CONFIRMED = "confirmed"    # 已确认，参与结论计算
STATUS_WITHDRAWN = "withdrawn"    # 已撤回，保留记录但不参与结论


@dataclass
class EvidenceRecord:
    evidence_id: str
    incident_id: str
    kind: str
    observed_at: datetime
    location_code: str
    payload: dict[str, Any]
    source: str
    registered_by: str
    registered_at: datetime
    is_backfill: bool
    backfill_reason: str | None
    content_hash: str
    seq: int
    status: str = STATUS_PENDING
    confirmations: list[dict[str, Any]] = field(default_factory=list)
    withdrawn: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        from .store import iso

        return {
            "evidence_id": self.evidence_id,
            "incident_id": self.incident_id,
            "kind": self.kind,
            "observed_at": iso(self.observed_at),
            "location_code": self.location_code,
            "payload": self.payload,
            "source": self.source,
            "registered_by": self.registered_by,
            "registered_at": iso(self.registered_at),
            "is_backfill": self.is_backfill,
            "backfill_reason": self.backfill_reason,
            "content_hash": self.content_hash,
            "status": self.status,
            "confirmations": list(self.confirmations),
            "withdrawn": self.withdrawn,
        }


@dataclass
class ReInspection:
    item_id: str
    result: str  # pass | fail
    inspector: str
    note: str
    evidence_id: str | None
    recorded_at: datetime
    seq: int

    def to_dict(self) -> dict[str, Any]:
        from .store import iso

        return {
            "item_id": self.item_id,
            "result": self.result,
            "inspector": self.inspector,
            "note": self.note,
            "evidence_id": self.evidence_id,
            "recorded_at": iso(self.recorded_at),
        }


@dataclass
class IncidentState:
    incident_id: str
    info: dict[str, Any] | None = None
    evidence: dict[str, EvidenceRecord] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)  # 证据登记顺序
    reinspections: list[ReInspection] = field(default_factory=list)
    reports: list[dict[str, Any]] = field(default_factory=list)

    def included_evidence(self) -> list[EvidenceRecord]:
        """参与结论计算的证据：已确认且未撤回。"""
        return [
            self.evidence[eid]
            for eid in self.order
            if self.evidence[eid].status == STATUS_CONFIRMED
        ]

    def excluded_evidence(self) -> list[EvidenceRecord]:
        return [
            self.evidence[eid]
            for eid in self.order
            if self.evidence[eid].status != STATUS_CONFIRMED
        ]


def replay(events: list[dict[str, Any]]) -> tuple[dict[str, IncidentState], dict[str, str]]:
    """回放事件流，返回 (事件状态表, 证据编号 -> 事件编号 索引)。"""
    states: dict[str, IncidentState] = {}
    evidence_index: dict[str, str] = {}
    for event in events:
        iid = event["incident_id"]
        state = states.setdefault(iid, IncidentState(incident_id=iid))
        etype = event["type"]
        data = event["data"]
        if etype == "incident_created":
            state.info = {
                "incident_id": iid,
                "title": data.get("title", ""),
                "description": data.get("description", ""),
                "created_by": event["actor"],
                "created_at": event["recorded_at"],
            }
        elif etype == "evidence_registered":
            record = EvidenceRecord(
                evidence_id=data["evidence_id"],
                incident_id=iid,
                kind=data["kind"],
                observed_at=parse_ts(data["observed_at"]),
                location_code=data["location_code"],
                payload=data.get("payload", {}),
                source=data.get("source", ""),
                registered_by=data.get("registered_by", ""),
                registered_at=parse_ts(data["registered_at"]),
                is_backfill=bool(data.get("is_backfill")),
                backfill_reason=data.get("backfill_reason"),
                content_hash=data["content_hash"],
                seq=event["seq"],
                status=data.get("initial_status", STATUS_PENDING),
            )
            state.evidence[record.evidence_id] = record
            state.order.append(record.evidence_id)
            evidence_index[record.evidence_id] = iid
        elif etype == "evidence_confirmed":
            record = state.evidence.get(data["evidence_id"])
            if record is not None:
                record.confirmations.append(
                    {
                        "confirmer": data["confirmer"],
                        "confirmation_no": data.get("confirmation_no"),
                        "at": event["recorded_at"],
                    }
                )
                if record.status == STATUS_PENDING and len(record.confirmations) >= 2:
                    record.status = STATUS_CONFIRMED
        elif etype == "evidence_withdrawn":
            record = state.evidence.get(data["evidence_id"])
            if record is not None:
                record.status = STATUS_WITHDRAWN
                record.withdrawn = {
                    "by": event["actor"],
                    "reason": data.get("reason", ""),
                    "at": event["recorded_at"],
                }
        elif etype == "reinspection_recorded":
            state.reinspections.append(
                ReInspection(
                    item_id=data["item_id"],
                    result=data["result"],
                    inspector=data.get("inspector", event["actor"]),
                    note=data.get("note", ""),
                    evidence_id=data.get("evidence_id"),
                    recorded_at=parse_ts(event["recorded_at"]),
                    seq=event["seq"],
                )
            )
        elif etype == "report_issued":
            state.reports.append(
                {
                    "report_id": data["report_id"],
                    "version": data["version"],
                    "digest": data["digest"],
                    "issued_at": event["recorded_at"],
                    "issued_by": event["actor"],
                }
            )
    return states, evidence_index
