"""证据台账：只追加、可撤回、补录需理由与双人确认，条目以哈希链串联。

台账是整个服务的可信时间线基础：
- 原始材料只追加不覆盖，撤回只是追加一条撤回记录，原始条目保留；
- 每条记录同时保存事件发生时间（observed_at）与入账时间（recorded_at），
  晚于事件建立或超过补录时限入账的记录视为补录，必须填写理由并经两名
  不同人员确认；
- 每条记录的 entry_hash 由前一条记录的哈希与自身内容计算，任何事后
  篡改都会破坏链条，可用 verify_chain() 校验。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .contracts import EvidenceKind


class LedgerError(ValueError):
    """台账操作的基础错误。"""


class BackfillError(LedgerError):
    """补录缺少理由或双人确认。"""


class DuplicateEvidenceError(LedgerError):
    """证据编号重复。"""


class UnknownEvidenceError(LedgerError):
    """证据不存在。"""


class PayloadError(LedgerError):
    """证据内容不符合对应类型的格式。"""


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _default(o: Any) -> Any:
    if isinstance(o, datetime):
        return _iso(o)
    if isinstance(o, EvidenceKind):
        return o.value
    if isinstance(o, tuple):
        return list(o)
    raise TypeError(f"无法序列化: {type(o)!r}")


def canonical_json(obj: Any) -> str:
    """生成稳定的 JSON 串（键排序、无空白），用于摘要与哈希链。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=_default)


def parse_datetime(value: Any) -> datetime:
    """解析 ISO 时间，必须带时区，否则抛 ValueError。"""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"时间格式非法: {value!r}") from exc
    else:
        raise ValueError(f"时间必须是 ISO 字符串或 datetime: {value!r}")
    if dt.tzinfo is None:
        raise ValueError("时间必须包含时区")
    return dt


WORK_ACTIONS = ("sealing", "pumping", "cleaning", "disinfection", "repair", "other")

_REQUIRED_FIELDS = {
    EvidenceKind.SENSOR: ("metric", "value", "limit"),
    EvidenceKind.INSPECTION: ("overflow_observed",),
    EvidenceKind.SAMPLE: ("analyte", "value", "limit"),
    EvidenceKind.WORK_RECEIPT: ("action", "completed"),
}


def validate_payload(kind: EvidenceKind, payload: Any) -> list[str]:
    """校验证据内容，返回错误列表，空列表表示通过。"""
    if not isinstance(payload, dict):
        return ["payload 必须是对象"]
    errors = [f"缺少字段 {name}" for name in _REQUIRED_FIELDS[kind] if name not in payload]
    if errors:
        return errors
    if kind in (EvidenceKind.SENSOR, EvidenceKind.SAMPLE):
        for name in ("value", "limit"):
            v = payload[name]
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                errors.append(f"{name} 必须是数值")
    if kind is EvidenceKind.INSPECTION and not isinstance(payload["overflow_observed"], bool):
        errors.append("overflow_observed 必须是布尔值")
    if kind is EvidenceKind.WORK_RECEIPT:
        if payload["action"] not in WORK_ACTIONS:
            errors.append(f"action 必须是 {WORK_ACTIONS} 之一")
        if not isinstance(payload["completed"], bool):
            errors.append("completed 必须是布尔值")
    return errors


@dataclass(frozen=True)
class BackfillMeta:
    """补录元信息：理由 + 两名不同确认人。"""

    reason: str
    confirmed_by: tuple[str, ...]

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.reason or not self.reason.strip():
            errors.append("补录必须填写理由")
        confirmers = [c.strip() for c in self.confirmed_by if c and c.strip()]
        if len(confirmers) != 2 or len(set(confirmers)) != 2:
            errors.append("补录必须由两名不同人员确认")
        return errors

    def to_dict(self) -> dict:
        return {"reason": self.reason, "confirmed_by": list(self.confirmed_by)}

    @classmethod
    def from_dict(cls, d: dict) -> "BackfillMeta":
        return cls(reason=str(d.get("reason", "")), confirmed_by=tuple(d.get("confirmed_by") or ()))


@dataclass(frozen=True)
class EvidenceRecord:
    """台账中的一条证据记录（不可变）。"""

    evidence_id: str
    incident_id: str
    kind: EvidenceKind
    observed_at: datetime
    recorded_at: datetime
    location_code: str
    payload: dict
    source: str
    sequence: int
    backfill: BackfillMeta | None
    entry_hash: str

    def to_dict(self) -> dict:
        return {
            "evidence_id": self.evidence_id,
            "incident_id": self.incident_id,
            "kind": self.kind.value,
            "observed_at": _iso(self.observed_at),
            "recorded_at": _iso(self.recorded_at),
            "location_code": self.location_code,
            "payload": self.payload,
            "source": self.source,
            "sequence": self.sequence,
            "backfill": self.backfill.to_dict() if self.backfill else None,
            "entry_hash": self.entry_hash,
        }

    def _content(self) -> dict:
        d = self.to_dict()
        del d["entry_hash"]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "EvidenceRecord":
        return cls(
            evidence_id=d["evidence_id"],
            incident_id=d["incident_id"],
            kind=EvidenceKind(d["kind"]),
            observed_at=parse_datetime(d["observed_at"]),
            recorded_at=parse_datetime(d["recorded_at"]),
            location_code=d["location_code"],
            payload=dict(d["payload"]),
            source=d.get("source", "manual"),
            sequence=int(d["sequence"]),
            backfill=BackfillMeta.from_dict(d["backfill"]) if d.get("backfill") else None,
            entry_hash=d["entry_hash"],
        )


@dataclass(frozen=True)
class Withdrawal:
    """撤回记录：追加在台账尾部，原始证据保留。"""

    evidence_id: str
    reason: str
    withdrawn_by: str
    withdrawn_at: datetime
    sequence: int

    def to_dict(self) -> dict:
        return {
            "evidence_id": self.evidence_id,
            "reason": self.reason,
            "withdrawn_by": self.withdrawn_by,
            "withdrawn_at": _iso(self.withdrawn_at),
            "sequence": self.sequence,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Withdrawal":
        return cls(
            evidence_id=d["evidence_id"],
            reason=d["reason"],
            withdrawn_by=d["withdrawn_by"],
            withdrawn_at=parse_datetime(d["withdrawn_at"]),
            sequence=int(d["sequence"]),
        )


class EvidenceLedger:
    """只追加的证据台账。"""

    def __init__(
        self,
        backfill_threshold: timedelta = timedelta(hours=24),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.backfill_threshold = backfill_threshold
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._records: list[EvidenceRecord] = []
        self._withdrawals: list[Withdrawal] = []
        self._incidents: dict[str, datetime] = {}

    # ---- 事件建立 ----
    def open_incident(self, incident_id: str, opened_at: datetime) -> None:
        opened_at = parse_datetime(opened_at)
        if incident_id in self._incidents:
            raise LedgerError(f"事件 {incident_id} 已存在")
        self._incidents[incident_id] = opened_at

    def incident_opened_at(self, incident_id: str) -> datetime:
        if incident_id not in self._incidents:
            raise LedgerError(f"事件 {incident_id} 不存在")
        return self._incidents[incident_id]

    def incidents(self) -> dict[str, datetime]:
        return dict(self._incidents)

    # ---- 追加 ----
    def append(
        self,
        *,
        incident_id: str,
        kind: EvidenceKind | str,
        observed_at: Any,
        location_code: str,
        payload: dict,
        evidence_id: str | None = None,
        source: str = "manual",
        recorded_at: Any = None,
        backfill: BackfillMeta | None = None,
    ) -> EvidenceRecord:
        opened_at = self.incident_opened_at(incident_id)
        kind = EvidenceKind(kind)
        observed_at = parse_datetime(observed_at)
        recorded_at = parse_datetime(recorded_at) if recorded_at is not None else self._clock()
        if not location_code or not str(location_code).strip():
            raise PayloadError("location_code 不能为空")
        errors = validate_payload(kind, payload)
        if errors:
            raise PayloadError("; ".join(errors))

        evidence_id = evidence_id or f"EV-{len(self._records) + 1:05d}"
        if any(r.evidence_id == evidence_id for r in self._records):
            raise DuplicateEvidenceError(f"证据编号 {evidence_id} 已存在")

        needs_backfill = (
            observed_at < opened_at
            or (recorded_at - observed_at) > self.backfill_threshold
        )
        if backfill is not None:
            errs = backfill.validate()
            if errs:
                raise BackfillError("; ".join(errs))
        elif needs_backfill:
            raise BackfillError(
                "该记录早于事件建立时间或超过补录时限，属于补录，"
                "必须填写补录理由并经两名不同人员确认"
            )

        sequence = len(self._records) + 1
        record = EvidenceRecord(
            evidence_id=evidence_id,
            incident_id=incident_id,
            kind=kind,
            observed_at=observed_at,
            recorded_at=recorded_at,
            location_code=str(location_code),
            payload=dict(payload),
            source=source,
            sequence=sequence,
            backfill=backfill,
            entry_hash="",
        )
        record = EvidenceRecord(**{**record.__dict__, "entry_hash": self._chain_hash(record)})
        self._records.append(record)
        return record

    def _chain_hash(self, record: EvidenceRecord) -> str:
        prev = self._records[-1].entry_hash if self._records else "GENESIS"
        material = prev + "|" + canonical_json(record._content())
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    # ---- 撤回 ----
    def withdraw(self, evidence_id: str, *, reason: str, withdrawn_by: str, at: Any = None) -> Withdrawal:
        self.get(evidence_id)
        if self.is_withdrawn(evidence_id):
            raise LedgerError(f"证据 {evidence_id} 已撤回，不能重复撤回")
        if not reason or not reason.strip():
            raise LedgerError("撤回必须填写理由")
        if not withdrawn_by or not withdrawn_by.strip():
            raise LedgerError("撤回必须填写操作人")
        w = Withdrawal(
            evidence_id=evidence_id,
            reason=reason,
            withdrawn_by=withdrawn_by,
            withdrawn_at=parse_datetime(at) if at is not None else self._clock(),
            sequence=len(self._withdrawals) + 1,
        )
        self._withdrawals.append(w)
        return w

    # ---- 查询 ----
    def get(self, evidence_id: str) -> EvidenceRecord:
        for r in self._records:
            if r.evidence_id == evidence_id:
                return r
        raise UnknownEvidenceError(f"证据 {evidence_id} 不存在")

    def is_withdrawn(self, evidence_id: str) -> bool:
        return any(w.evidence_id == evidence_id for w in self._withdrawals)

    def withdrawal_of(self, evidence_id: str) -> Withdrawal | None:
        for w in self._withdrawals:
            if w.evidence_id == evidence_id:
                return w
        return None

    def records(self, incident_id: str, *, include_withdrawn: bool = False) -> list[EvidenceRecord]:
        out = [r for r in self._records if r.incident_id == incident_id]
        if not include_withdrawn:
            out = [r for r in out if not self.is_withdrawn(r.evidence_id)]
        return out

    def withdrawals(self, incident_id: str | None = None) -> list[Withdrawal]:
        if incident_id is None:
            return list(self._withdrawals)
        ids = {r.evidence_id for r in self._records if r.incident_id == incident_id}
        return [w for w in self._withdrawals if w.evidence_id in ids]

    def timeline(self, incident_id: str) -> list[EvidenceRecord]:
        """可信时间线：按事件发生时间排序，同刻按入账序号。"""
        recs = self.records(incident_id, include_withdrawn=True)
        return sorted(recs, key=lambda r: (r.observed_at, r.sequence))

    def verify_chain(self) -> bool:
        """重放哈希链，任何条目被篡改都会返回 False。"""
        prev = "GENESIS"
        for r in self._records:
            material = prev + "|" + canonical_json(r._content())
            if hashlib.sha256(material.encode("utf-8")).hexdigest() != r.entry_hash:
                return False
            prev = r.entry_hash
        return True

    # ---- 序列化 ----
    def dump(self) -> dict:
        return {
            "incidents": {k: _iso(v) for k, v in self._incidents.items()},
            "records": [r.to_dict() for r in self._records],
            "withdrawals": [w.to_dict() for w in self._withdrawals],
        }

    def load(self, data: dict) -> None:
        self._incidents = {k: parse_datetime(v) for k, v in data.get("incidents", {}).items()}
        self._records = [EvidenceRecord.from_dict(d) for d in data.get("records", [])]
        self._withdrawals = [Withdrawal.from_dict(d) for d in data.get("withdrawals", [])]
