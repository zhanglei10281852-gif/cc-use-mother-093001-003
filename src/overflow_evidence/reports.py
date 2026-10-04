"""报告：签发即不可变的结论快照、可校验摘要、历次版本差异。

- 每次签发生成一份 Report，内容快照 + sha256 摘要，签发后不再修改；
- 撤回证据后重新签发会得到新版本，旧版本完整保留；
- verify 通过重新计算摘要校验报告未被篡改；
- diff 给出两版报告在证据、区段、整改三个维度上的差异。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .ledger import canonical_json, parse_datetime


class ReportError(ValueError):
    """报告操作非法。"""


def compute_digest(snapshot: dict) -> str:
    """对报告内容快照计算可校验摘要。"""
    return hashlib.sha256(canonical_json(snapshot).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Report:
    report_id: str
    incident_id: str
    version: int
    generated_at: datetime
    snapshot: dict
    digest: str

    def verify(self) -> bool:
        return compute_digest(self.snapshot) == self.digest

    def summary(self) -> dict:
        return {
            "report_id": self.report_id,
            "incident_id": self.incident_id,
            "version": self.version,
            "generated_at": self.generated_at.isoformat(),
            "digest": self.digest,
            "segments": sorted(self.snapshot.get("segments", {}).keys()),
            "remediations": sorted(self.snapshot.get("remediations", {}).keys()),
        }

    def to_dict(self) -> dict:
        return {
            **self.summary(),
            "snapshot": self.snapshot,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Report":
        return cls(
            report_id=d["report_id"],
            incident_id=d["incident_id"],
            version=int(d["version"]),
            generated_at=parse_datetime(d["generated_at"]),
            snapshot=d["snapshot"],
            digest=d["digest"],
        )


class ReportStore:
    """报告仓库：只增不改。"""

    def __init__(self) -> None:
        self._reports: dict[str, Report] = {}

    def add(self, report: Report) -> None:
        if report.report_id in self._reports:
            raise ReportError(f"报告 {report.report_id} 已存在，报告签发后不可覆盖")
        self._reports[report.report_id] = report

    def get(self, report_id: str) -> Report:
        if report_id not in self._reports:
            raise ReportError(f"报告 {report_id} 不存在")
        return self._reports[report_id]

    def list(self, incident_id: str | None = None) -> list[Report]:
        reps = list(self._reports.values())
        if incident_id is not None:
            reps = [r for r in reps if r.incident_id == incident_id]
        return sorted(reps, key=lambda r: (r.incident_id, r.version))

    def next_version(self, incident_id: str) -> int:
        versions = [r.version for r in self._reports.values() if r.incident_id == incident_id]
        return max(versions, default=0) + 1

    def dump(self) -> dict:
        return {"reports": [r.to_dict() for r in self.list()]}

    def load(self, data: dict) -> None:
        self._reports = {}
        for d in data.get("reports", []):
            r = Report.from_dict(d)
            self._reports[r.report_id] = r


def _diff_maps(before: dict, after: dict) -> dict:
    added = sorted(k for k in after if k not in before)
    removed = sorted(k for k in before if k not in after)
    changed = {
        k: {"before": before[k], "after": after[k]}
        for k in before.keys() & after.keys()
        if before[k] != after[k]
    }
    return {"added": added, "removed": removed, "changed": changed}


def diff_snapshots(old: dict, new: dict) -> dict:
    """比较两版报告快照，给出证据、区段、整改三个维度的差异。"""
    old_ev = old.get("evidence", {})
    new_ev = new.get("evidence", {})
    return {
        "evidence": {
            "activated": sorted(set(new_ev.get("active", [])) - set(old_ev.get("active", []))),
            "withdrawn": sorted(set(new_ev.get("withdrawn", [])) - set(old_ev.get("withdrawn", []))),
        },
        "segments": _diff_maps(old.get("segments", {}), new.get("segments", {})),
        "remediations": _diff_maps(old.get("remediations", {}), new.get("remediations", {})),
    }
