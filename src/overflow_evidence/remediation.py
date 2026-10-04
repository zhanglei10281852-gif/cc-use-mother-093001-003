"""整改任务台账：状态机、复验记录、逾期升级。

整改项由证据派生（见 analysis.derive_conclusions），本模块把派生结果
同步进持久注册表并跟踪后续闭环过程：
- 证据满足完成条件 → SATISFIED（待复验）；证据被撤回后自动回退 OPEN；
- 复验通过 → VERIFIED，前提是条件已满足且全部依赖项已 VERIFIED；
- 复验不通过 → 回退 OPEN 并保留复验记录；
- 超过期限未 VERIFIED → ESCALATED，升级记录逐次累积。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from .analysis import RemediationConclusion
from .ledger import parse_datetime


class RemediationError(ValueError):
    """整改操作非法。"""


class TaskStatus(StrEnum):
    OPEN = "open"
    SATISFIED = "satisfied"
    VERIFIED = "verified"
    ESCALATED = "escalated"


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


@dataclass(frozen=True)
class Reinspection:
    evidence_id: str
    passed: bool
    inspector: str
    at: datetime
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "evidence_id": self.evidence_id,
            "passed": self.passed,
            "inspector": self.inspector,
            "at": _iso(self.at),
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Reinspection":
        return cls(d["evidence_id"], bool(d["passed"]), d["inspector"],
                   parse_datetime(d["at"]), d.get("note", ""))


@dataclass(frozen=True)
class Escalation:
    level: int
    at: datetime
    reason: str

    def to_dict(self) -> dict:
        return {"level": self.level, "at": _iso(self.at), "reason": self.reason}

    @classmethod
    def from_dict(cls, d: dict) -> "Escalation":
        return cls(int(d["level"]), parse_datetime(d["at"]), d["reason"])


@dataclass
class RemediationTask:
    task_id: str  # f"{incident_id}:{key}"
    incident_id: str
    key: str
    location_code: str
    title: str
    condition: str
    due_at: datetime
    depends_on: list[str]  # task_id 列表
    status: TaskStatus = TaskStatus.OPEN
    satisfied: bool = False
    evidence_ids: list[str] = field(default_factory=list)
    reinspections: list[Reinspection] = field(default_factory=list)
    escalations: list[Escalation] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "incident_id": self.incident_id,
            "key": self.key,
            "location_code": self.location_code,
            "title": self.title,
            "condition": self.condition,
            "due_at": _iso(self.due_at),
            "depends_on": list(self.depends_on),
            "status": self.status.value,
            "satisfied": self.satisfied,
            "evidence_ids": list(self.evidence_ids),
            "reinspections": [r.to_dict() for r in self.reinspections],
            "escalations": [e.to_dict() for e in self.escalations],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "RemediationTask":
        return cls(
            task_id=d["task_id"],
            incident_id=d["incident_id"],
            key=d["key"],
            location_code=d["location_code"],
            title=d["title"],
            condition=d["condition"],
            due_at=parse_datetime(d["due_at"]),
            depends_on=list(d.get("depends_on", [])),
            status=TaskStatus(d.get("status", "open")),
            satisfied=bool(d.get("satisfied", False)),
            evidence_ids=list(d.get("evidence_ids", [])),
            reinspections=[Reinspection.from_dict(x) for x in d.get("reinspections", [])],
            escalations=[Escalation.from_dict(x) for x in d.get("escalations", [])],
        )


class RemediationRegistry:
    """整改任务注册表：同步派生项、复验、逾期升级。"""

    def __init__(self) -> None:
        self._tasks: dict[str, RemediationTask] = {}

    # ---- 同步 ----
    def sync(self, incident_id: str, derived: list[RemediationConclusion]) -> list[RemediationTask]:
        """把最新派生结果合并进注册表，返回该事件的全部任务。"""
        out: list[RemediationTask] = []
        for d in derived:
            task_id = f"{incident_id}:{d.key}"
            t = self._tasks.get(task_id)
            if t is None:
                t = RemediationTask(
                    task_id=task_id,
                    incident_id=incident_id,
                    key=d.key,
                    location_code=d.location_code,
                    title=d.title,
                    condition=d.condition,
                    due_at=d.due_at,
                    depends_on=[f"{incident_id}:{dep}" for dep in d.depends_on],
                )
                self._tasks[task_id] = t
            t.satisfied = d.satisfied
            t.evidence_ids = list(d.evidence_ids)
            if t.status is not TaskStatus.VERIFIED:
                if d.satisfied and t.status in (TaskStatus.OPEN, TaskStatus.ESCALATED):
                    t.status = TaskStatus.SATISFIED
                elif not d.satisfied and t.status is TaskStatus.SATISFIED:
                    t.status = TaskStatus.OPEN
            out.append(t)
        return out

    # ---- 查询 ----
    def get(self, task_id: str) -> RemediationTask:
        if task_id not in self._tasks:
            raise RemediationError(f"整改项 {task_id} 不存在")
        return self._tasks[task_id]

    def for_incident(self, incident_id: str) -> list[RemediationTask]:
        return sorted(
            (t for t in self._tasks.values() if t.incident_id == incident_id),
            key=lambda t: t.task_id,
        )

    # ---- 复验 ----
    def record_reinspection(
        self,
        task_id: str,
        *,
        evidence_id: str,
        passed: bool,
        inspector: str,
        at: Any = None,
        note: str = "",
    ) -> Reinspection:
        t = self.get(task_id)
        if not inspector or not inspector.strip():
            raise RemediationError("复验必须填写复验人")
        if t.status is TaskStatus.VERIFIED:
            raise RemediationError(f"整改项 {task_id} 已复验通过，不能重复复验")
        at_dt = parse_datetime(at) if at is not None else datetime.now(timezone.utc)
        if passed:
            if not t.satisfied:
                raise RemediationError("完成条件尚未由证据满足，不能登记复验通过")
            unmet = [d for d in t.depends_on
                     if d in self._tasks and self._tasks[d].status is not TaskStatus.VERIFIED]
            if unmet:
                raise RemediationError(f"依赖整改项尚未复验通过: {', '.join(unmet)}")
        rec = Reinspection(evidence_id=evidence_id, passed=passed,
                           inspector=inspector, at=at_dt, note=note)
        t.reinspections.append(rec)
        t.status = TaskStatus.VERIFIED if passed else TaskStatus.OPEN
        return rec

    # ---- 逾期升级 ----
    def check_overdue(self, now: Any = None) -> list[dict]:
        """对逾期未闭环的任务升级；已处于 ESCALATED 的不重复升级。"""
        now_dt = parse_datetime(now) if now is not None else datetime.now(timezone.utc)
        out: list[dict] = []
        for t in sorted(self._tasks.values(), key=lambda x: x.task_id):
            if t.status in (TaskStatus.VERIFIED, TaskStatus.ESCALATED):
                continue
            if now_dt > t.due_at:
                esc = Escalation(
                    level=len(t.escalations) + 1,
                    at=now_dt,
                    reason=f"超过期限 {_iso(t.due_at)} 未闭环",
                )
                t.escalations.append(esc)
                t.status = TaskStatus.ESCALATED
                out.append({"task_id": t.task_id, **esc.to_dict()})
        return out

    # ---- 完成条件视图 ----
    def completion(self, task_id: str, now: Any = None) -> dict:
        t = self.get(task_id)
        now_dt = parse_datetime(now) if now is not None else datetime.now(timezone.utc)
        deps = []
        for dep_id in t.depends_on:
            dep = self._tasks.get(dep_id)
            deps.append({
                "task_id": dep_id,
                "status": dep.status.value if dep else "missing",
                "met": bool(dep and dep.status is TaskStatus.VERIFIED),
            })
        deps_met = all(d["met"] for d in deps)
        return {
            **t.to_dict(),
            "dependencies": deps,
            "dependencies_met": deps_met,
            "overdue": t.status is not TaskStatus.VERIFIED and now_dt > t.due_at,
            "closable": t.satisfied and deps_met and t.status is not TaskStatus.VERIFIED,
        }

    # ---- 序列化 ----
    def dump(self) -> dict:
        return {"tasks": [t.to_dict() for t in self._tasks.values()]}

    def load(self, data: dict) -> None:
        self._tasks = {t.task_id: t for t in
                       (RemediationTask.from_dict(d) for d in data.get("tasks", []))}
