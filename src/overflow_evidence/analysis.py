"""结论推导：由有效证据计算影响区段、处置阶段与整改项。

推导规则（全部可由证据复核）：
- 超标判定：传感/样本读数 value > limit，或人工巡查明确观察到冒溢；
- 影响区段：任一有效证据出现超标的 location_code；
- 处置阶段按 detection → containment → cleaning → restoration → verification
  推进，作业回执必须发生在首次超标之后才计入对应阶段；
- verification 要求存在晚于最后一次超标的合格传感/样本读数；
- 每个影响区段派生四项整改（封控、清掏、修复、复验），带依赖与期限。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .contracts import EvidenceKind
from .ledger import EvidenceRecord

STAGES = ("detection", "containment", "cleaning", "restoration", "verification")

# 各阶段对应的作业回执 action
STAGE_ACTIONS = {
    "containment": {"sealing"},
    "cleaning": {"pumping", "cleaning"},
    "restoration": {"repair", "disinfection"},
}

# 整改项元数据：标题、完成条件、对应处置阶段、依赖
REMEDIATION_PLAN = (
    ("contain", "围挡封控", "取得已完成的封控作业回执（sealing）", "containment", ()),
    ("clean", "抽排清掏", "取得已完成的抽排/清掏作业回执（pumping/cleaning）", "cleaning", ("contain",)),
    ("restore", "修复消杀", "取得已完成的修复/消杀作业回执（repair/disinfection）", "restoration", ("clean",)),
    ("verify", "复验确认", "取得晚于末次超标的合格传感或样本读数", "verification", ("restore",)),
)


@dataclass(frozen=True)
class SlaConfig:
    """各整改项的期限（小时，相对首次超标时间）。"""

    containment_hours: float = 4.0
    cleaning_hours: float = 24.0
    restoration_hours: float = 48.0
    verification_hours: float = 72.0

    def hours_for(self, prefix: str) -> float:
        return {
            "contain": self.containment_hours,
            "clean": self.cleaning_hours,
            "restore": self.restoration_hours,
            "verify": self.verification_hours,
        }[prefix]


def is_exceedance(rec: EvidenceRecord) -> bool:
    """该证据是否表明污染存在（超标或观察到冒溢）。"""
    p = rec.payload
    if rec.kind in (EvidenceKind.SENSOR, EvidenceKind.SAMPLE):
        return p["value"] > p["limit"]
    if rec.kind is EvidenceKind.INSPECTION:
        return bool(p["overflow_observed"])
    return False


def is_clearance(rec: EvidenceRecord) -> bool:
    """该证据是否为合格读数（可用于复验）。"""
    return rec.kind in (EvidenceKind.SENSOR, EvidenceKind.SAMPLE) and not is_exceedance(rec)


@dataclass
class StageConclusion:
    stage: str
    reached: bool
    reached_at: datetime | None
    evidence_ids: list[str]

    def to_dict(self) -> dict:
        return {
            "stage": self.stage,
            "reached": self.reached,
            "reached_at": self.reached_at.isoformat() if self.reached_at else None,
            "evidence_ids": list(self.evidence_ids),
        }


@dataclass
class SegmentConclusion:
    location_code: str
    first_detected_at: datetime
    last_exceedance_at: datetime
    evidence_ids: list[str]
    exceedance_ids: list[str]
    stages: list[StageConclusion]

    @property
    def current_stage(self) -> str:
        reached = [s for s in self.stages if s.reached]
        return reached[-1].stage if reached else "none"

    def stage(self, name: str) -> StageConclusion:
        for s in self.stages:
            if s.stage == name:
                return s
        raise KeyError(name)

    def to_dict(self) -> dict:
        return {
            "location_code": self.location_code,
            "first_detected_at": self.first_detected_at.isoformat(),
            "last_exceedance_at": self.last_exceedance_at.isoformat(),
            "current_stage": self.current_stage,
            "evidence_ids": list(self.evidence_ids),
            "exceedance_ids": list(self.exceedance_ids),
            "stages": [s.to_dict() for s in self.stages],
        }


@dataclass
class RemediationConclusion:
    """由证据派生的整改项（key 形如 contain:BLOCK-2）。"""

    key: str
    location_code: str
    title: str
    condition: str
    due_at: datetime
    depends_on: list[str]
    satisfied: bool
    evidence_ids: list[str]

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "location_code": self.location_code,
            "title": self.title,
            "condition": self.condition,
            "due_at": self.due_at.isoformat(),
            "depends_on": list(self.depends_on),
            "satisfied": self.satisfied,
            "evidence_ids": list(self.evidence_ids),
        }


@dataclass
class Conclusions:
    segments: list[SegmentConclusion] = field(default_factory=list)
    remediations: list[RemediationConclusion] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "segments": {s.location_code: s.to_dict() for s in self.segments},
            "remediations": {r.key: r.to_dict() for r in self.remediations},
        }


def derive_conclusions(records: list[EvidenceRecord], sla: SlaConfig | None = None) -> Conclusions:
    """从一组有效证据推导结论。records 应已剔除撤回证据。"""
    sla = sla or SlaConfig()
    by_loc: dict[str, list[EvidenceRecord]] = {}
    for r in records:
        by_loc.setdefault(r.location_code, []).append(r)

    segments: list[SegmentConclusion] = []
    remediations: list[RemediationConclusion] = []

    for loc in sorted(by_loc):
        recs = sorted(by_loc[loc], key=lambda r: (r.observed_at, r.sequence))
        exceedances = [r for r in recs if is_exceedance(r)]
        if not exceedances:
            continue
        first_at = exceedances[0].observed_at
        last_at = exceedances[-1].observed_at

        stages: list[StageConclusion] = [
            StageConclusion("detection", True, first_at, [exceedances[0].evidence_id])
        ]
        for stage, actions in STAGE_ACTIONS.items():
            hits = [
                r for r in recs
                if r.kind is EvidenceKind.WORK_RECEIPT
                and r.payload["completed"]
                and r.payload["action"] in actions
                and r.observed_at >= first_at
            ]
            stages.append(StageConclusion(
                stage, bool(hits),
                hits[0].observed_at if hits else None,
                [h.evidence_id for h in hits],
            ))
        clearances = [r for r in recs if is_clearance(r) and r.observed_at > last_at]
        stages.append(StageConclusion(
            "verification", bool(clearances),
            clearances[0].observed_at if clearances else None,
            [c.evidence_id for c in clearances],
        ))

        segments.append(SegmentConclusion(
            location_code=loc,
            first_detected_at=first_at,
            last_exceedance_at=last_at,
            evidence_ids=[r.evidence_id for r in recs],
            exceedance_ids=[r.evidence_id for r in exceedances],
            stages=stages,
        ))

        for prefix, title, condition, stage_name, deps in REMEDIATION_PLAN:
            stage = stages[STAGES.index(stage_name)]
            remediations.append(RemediationConclusion(
                key=f"{prefix}:{loc}",
                location_code=loc,
                title=f"{loc} {title}",
                condition=condition,
                due_at=first_at + timedelta(hours=sla.hours_for(prefix)),
                depends_on=[f"{d}:{loc}" for d in deps],
                satisfied=stage.reached,
                evidence_ids=list(stage.evidence_ids),
            ))

    return Conclusions(segments=segments, remediations=remediations)
