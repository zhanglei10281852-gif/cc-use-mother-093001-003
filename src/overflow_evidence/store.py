"""追加式事件存储。

所有事实（证据登记、确认、撤回、复验、报告签发）以哈希链事件写入
``events.jsonl``，只追加、不覆盖、不删除；每条事件携带序号、登记时间、
前序哈希与自身哈希，构成可校验的可信时间线。批量导入被拒的记录写入
独立的 ``quarantine.jsonl``，绝不混入主日志。
"""
from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

GENESIS_HASH = "0" * 64


def canonical_json(obj: Any) -> str:
    """确定性 JSON 序列化，用于哈希计算。"""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def parse_ts(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError("时间必须包含时区")
    return dt


class EventStore:
    """单文件追加式事件日志 + 报告文件柜。"""

    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.data_dir / "events.jsonl"
        self.quarantine_path = self.data_dir / "quarantine.jsonl"
        self.reports_dir = self.data_dir / "reports"
        self.reports_dir.mkdir(exist_ok=True)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ events
    def append(
        self,
        event_type: str,
        incident_id: str,
        actor: str,
        data: dict[str, Any],
        recorded_at: datetime | None = None,
    ) -> dict[str, Any]:
        """追加一条事件并返回完整记录（含序号与哈希）。"""
        with self._lock:
            last = self._last_event()
            seq = last["seq"] + 1 if last else 1
            event: dict[str, Any] = {
                "seq": seq,
                "event_id": f"EV-{seq:08d}",
                "type": event_type,
                "incident_id": incident_id,
                "actor": actor,
                "recorded_at": iso(recorded_at or utcnow()),
                "data": data,
                "prev_hash": last["hash"] if last else GENESIS_HASH,
            }
            event["hash"] = sha256_text(canonical_json(event))
            with self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False) + "\n")
            return event

    def events(self, incident_id: str | None = None) -> list[dict[str, Any]]:
        result = list(self._iter_events())
        if incident_id is not None:
            result = [e for e in result if e["incident_id"] == incident_id]
        return result

    def _iter_events(self) -> Iterable[dict[str, Any]]:
        if not self.log_path.exists():
            return
        with self.log_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    yield json.loads(line)

    def _last_event(self) -> dict[str, Any] | None:
        last = None
        for event in self._iter_events():
            last = event
        return last

    def verify_chain(self) -> dict[str, Any]:
        """重放整条日志，校验序号连续、哈希衔接与内容未被篡改。"""
        errors: list[str] = []
        checked = 0
        prev_hash = GENESIS_HASH
        for event in self._iter_events():
            checked += 1
            if event["seq"] != checked:
                errors.append(f"序号断裂：期望 {checked}，实际 {event['seq']}")
            if event["prev_hash"] != prev_hash:
                errors.append(f"哈希链断裂于序号 {event['seq']}")
            body = {k: v for k, v in event.items() if k != "hash"}
            if sha256_text(canonical_json(body)) != event["hash"]:
                errors.append(f"事件 {event['event_id']} 内容哈希不匹配")
            prev_hash = event["hash"]
        return {"valid": not errors, "checked": checked, "errors": errors}

    # ----------------------------------------------------------------- reports
    def save_report(self, report: dict[str, Any]) -> Path:
        """报告一经签发即不可变：已存在同名文件时拒绝覆盖。"""
        path = self.reports_dir / f"{report['report_id']}.json"
        with path.open("x", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
        return path

    def load_report(self, report_id: str) -> dict[str, Any] | None:
        if not _SAFE_ID.match(report_id):
            return None
        path = self.reports_dir / f"{report_id}.json"
        if not path.exists():
            return None
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)

    # -------------------------------------------------------------- quarantine
    def quarantine(self, entry: dict[str, Any]) -> None:
        """记录被拒的导入行（含原因），与主日志物理隔离。"""
        with self._lock:
            with self.quarantine_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def quarantined(self) -> list[dict[str, Any]]:
        if not self.quarantine_path.exists():
            return []
        with self.quarantine_path.open("r", encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]


import re

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,120}$")
