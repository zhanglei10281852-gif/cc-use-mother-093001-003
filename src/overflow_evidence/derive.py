"""由证据推导结论：影响区段、处置阶段、整改项与逾期升级。

推导是纯函数：输入为"已确认且未撤回"的证据与复验记录，输出确定性的
结论结构。撤回证据后重新执行推导即得到新结论，旧报告不受影响。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .models import EvidenceRecord, ReInspection
from .store import iso

# 传感器默认限值（payload 未显式给出 threshold 时使用）
DEFAULT_SENSOR_THRESHOLDS = {"cod": 30.0, "ammonia": 1.5, "turbidity": 50.0}

# 作业回执动作分类
CONTAINMENT_ACTIONS = {"road_closure"}
REMEDIATION_ACTIONS = {"cleaning", "dredging", "repair", "disinfection"}
CLEANING_ACTIONS = {"cleaning", "dredging"}

# 整改时限（自区段首次检出污染起算）
DUE_CLEANING = timedelta(days=3)
DUE_REPAIR = timedelta(days=7)
DUE_REINSPECTION = timedelta(days=10)
DUE_REINSPECTION_WITH_REPAIR = timedelta(days=14)

ESCALATION_LABELS = {1: "提醒", 2: "督办", 3: "挂牌"}

ITEM_TITLES = {"cleaning": "清掏作业", "repair": "管网检修", "reinspection": "复验确认"}


def evaluate(record: EvidenceRecord) -> dict[str, Any]:
    """判定单条证据是否指示污染（positive）或指示已恢复（clearing）。"""
    payload = record.payload
    if record.kind == "sensor":
        value = payload.get("value")
        threshold = payload.get("threshold", DEFAULT_SENSOR_THRESHOLDS.get(payload.get("parameter")))
        if isinstance(value, (int, float)) and isinstance(threshold, (int, float)) and threshold > 0:
            return {
                "evaluable": True,
                "positive": value > threshold,
                "clearing": value <= threshold,
                "ratio": round(value / threshold, 4),
            }
        return {"evaluable": False, "positive": False, "clearing": False, "ratio": None}
    if record.kind == "sample":
        value = payload.get("value")
        limit = payload.get("limit")
        return {
            "evaluable": True,
            "positive": value > limit,
            "clearing": value <= limit,
            "ratio": round(value / limit, 4),
        }
    if record.kind == "inspection":
        positive = bool(payload.get("overflow_observed"))
        return {"evaluable": True, "positive": positive, "clearing": not positive, "ratio": 1.0 if positive else 0.0}
    return {"evaluable": False, "positive": False, "clearing": False, "ratio": None}  # work_receipt


def _by_observed(records: list[EvidenceRecord]) -> list[EvidenceRecord]:
    return sorted(records, key=lambda r: (r.observed_at, r.seq))


def derive_impact_zones(evidence: list[EvidenceRecord]) -> list[dict[str, Any]]:
    """按位置聚合污染证据，生成影响区段。"""
    by_location: dict[str, list[EvidenceRecord]] = {}
    for record in evidence:
        by_location.setdefault(record.location_code, []).append(record)

    zones: list[dict[str, Any]] = []
    for location in sorted(by_location):
        records = _by_observed(by_location[location])
        verdicts = {r.evidence_id: evaluate(r) for r in records}
        positives = [r for r in records if verdicts[r.evidence_id]["positive"]]
        if not positives:
            continue
        first_detected = positives[0].observed_at
        last_positive = positives[-1].observed_at
        clearing_after = [
            r for r in records if verdicts[r.evidence_id]["clearing"] and r.observed_at > last_positive
        ]
        cleared_at = clearing_after[0].observed_at if clearing_after else None
        score = round(sum(min(verdicts[r.evidence_id]["ratio"] or 0.0, 3.0) for r in positives), 3)
        severity = "severe" if score >= 4.0 else "moderate" if score >= 1.5 else "minor"
        zones.append(
            {
                "zone_id": f"ZONE-{location}",
                "location_code": location,
                "status": "cleared" if cleared_at else "active",
                "severity": severity,
                "score": score,
                "first_detected_at": iso(first_detected),
                "last_positive_at": iso(last_positive),
                "cleared_at": iso(cleared_at) if cleared_at else None,
                "positive_evidence_ids": [r.evidence_id for r in positives],
                "evidence_ids": [r.evidence_id for r in records],
            }
        )
    return zones


def derive_phases(evidence: list[EvidenceRecord], zones: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按证据时间线推导处置阶段：发现 → 封控 → 处置 → 复验。"""
    verdicts = {r.evidence_id: evaluate(r) for r in evidence}
    positives = _by_observed([r for r in evidence if verdicts[r.evidence_id]["positive"]])
    closures = _by_observed(
        [r for r in evidence if r.kind == "work_receipt" and r.payload.get("action") in CONTAINMENT_ACTIONS]
    )
    remedies = _by_observed(
        [r for r in evidence if r.kind == "work_receipt" and r.payload.get("action") in REMEDIATION_ACTIONS]
    )
    clearings = _by_observed([r for r in evidence if verdicts[r.evidence_id]["clearing"]])

    detection_at = positives[0].observed_at if positives else None
    containment_at = closures[0].observed_at if closures else None
    remediation_at = remedies[0].observed_at if remedies else None
    verify_base = remediation_at or detection_at
    verification_candidates = [r for r in clearings if verify_base and r.observed_at > verify_base]
    verification_at = verification_candidates[0].observed_at if verification_candidates else None

    all_cleared = bool(zones) and all(z["status"] == "cleared" for z in zones)
    verification_done_at = (
        max(datetime.fromisoformat(z["cleared_at"]) for z in zones) if all_cleared else None
    )

    def phase(name: str, title: str, started, completed, evidence_ids):
        return {
            "name": name,
            "title": title,
            "status": "not_started" if started is None else ("completed" if completed else "in_progress"),
            "started_at": iso(started) if started else None,
            "completed_at": iso(completed) if completed else None,
            "evidence_ids": evidence_ids,
        }

    return [
        phase("detection", "污染发现", detection_at, containment_at, [positives[0].evidence_id] if positives else []),
        phase("containment", "围挡封控", containment_at, remediation_at, [r.evidence_id for r in closures]),
        phase("remediation", "清掏处置", remediation_at, verification_at, [r.evidence_id for r in remedies]),
        phase(
            "verification",
            "复验确认",
            verification_at,
            verification_done_at,
            [r.evidence_id for r in verification_candidates],
        ),
    ]


def _escalation(due_at: datetime, now: datetime) -> tuple[bool, int, int]:
    days_overdue = int((now - due_at).total_seconds() // 86400)
    if days_overdue < 0:
        return False, 0, days_overdue
    level = 1 if days_overdue <= 2 else (2 if days_overdue <= 6 else 3)
    return True, level, days_overdue


def derive_remediation(
    incident_id: str,
    zones: list[dict[str, Any]],
    evidence: list[EvidenceRecord],
    reinspections: list[ReInspection],
    now: datetime,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """为每个影响区段生成整改项，并计算依赖、复验与逾期状态。

    返回 (整改项列表, 孤儿复验记录列表)。孤儿复验指向已不存在的整改项
    （例如支撑区段的证据被撤回），保留展示以便审计。
    """
    receipts_by_location: dict[str, list[EvidenceRecord]] = {}
    for record in evidence:
        if record.kind == "work_receipt":
            receipts_by_location.setdefault(record.location_code, []).append(record)

    items: list[dict[str, Any]] = []
    generated_ids: set[str] = set()
    for zone in zones:
        location = zone["location_code"]
        detected = datetime.fromisoformat(zone["first_detected_at"])
        receipts = _by_observed(receipts_by_location.get(location, []))
        severe = zone["severity"] == "severe"

        clean_id = f"REM-{incident_id}-{location}-CLEAN"
        repair_id = f"REM-{incident_id}-{location}-REPAIR"
        reinspect_id = f"REM-{incident_id}-{location}-VERIFY"

        def receipt_completion(actions: set[str]) -> tuple[bool, str | None, list[str]]:
            hits = [
                r
                for r in receipts
                if r.payload.get("action") in actions and r.observed_at >= detected
            ]
            done = bool(hits)
            return done, (iso(hits[0].observed_at) if done else None), [r.evidence_id for r in hits]

        specs: list[dict[str, Any]] = []
        clean_done, clean_at, clean_eids = receipt_completion(CLEANING_ACTIONS)
        specs.append(
            {
                "item_id": clean_id,
                "kind": "cleaning",
                "depends_on": [],
                "due_at": detected + DUE_CLEANING,
                "completion_conditions": {
                    "type": "work_receipt",
                    "actions": sorted(CLEANING_ACTIONS),
                    "location_code": location,
                    "not_before": iso(detected),
                },
                "conditions_met": clean_done,
                "condition_met_at": clean_at,
                "evidence_ids": clean_eids,
            }
        )
        if severe:
            repair_done, repair_at, repair_eids = receipt_completion({"repair"})
            specs.append(
                {
                    "item_id": repair_id,
                    "kind": "repair",
                    "depends_on": [clean_id],
                    "due_at": detected + DUE_REPAIR,
                    "completion_conditions": {
                        "type": "work_receipt",
                        "actions": ["repair"],
                        "location_code": location,
                        "not_before": iso(detected),
                    },
                    "conditions_met": repair_done,
                    "condition_met_at": repair_at,
                    "evidence_ids": repair_eids,
                }
            )
        verify_deps = [clean_id] + ([repair_id] if severe else [])
        passes = [r for r in reinspections if r.item_id == reinspect_id and r.result == "pass"]
        specs.append(
            {
                "item_id": reinspect_id,
                "kind": "reinspection",
                "depends_on": verify_deps,
                "due_at": detected + (DUE_REINSPECTION_WITH_REPAIR if severe else DUE_REINSPECTION),
                "completion_conditions": {
                    "type": "passing_reinspection",
                    "item_id": reinspect_id,
                    "requires_dependencies_completed": True,
                },
                "conditions_met": bool(passes),
                "condition_met_at": iso(passes[0].recorded_at) if passes else None,
                "evidence_ids": [r.evidence_id for r in passes if r.evidence_id],
            }
        )

        done_by_id: dict[str, bool] = {}
        for spec in specs:
            deps = spec["depends_on"]
            blocked_by = [d for d in deps if not done_by_id.get(d, False)]
            completed = spec["conditions_met"] and not blocked_by
            done_by_id[spec["item_id"]] = completed
            overdue, level, days = _escalation(spec["due_at"], now)
            history = [r.to_dict() for r in reinspections if r.item_id == spec["item_id"]]
            items.append(
                {
                    "item_id": spec["item_id"],
                    "incident_id": incident_id,
                    "zone_id": zone["zone_id"],
                    "location_code": location,
                    "kind": spec["kind"],
                    "title": ITEM_TITLES[spec["kind"]],
                    "depends_on": deps,
                    "blocked_by": blocked_by,
                    "status": "completed" if completed else ("blocked" if blocked_by else "in_progress"),
                    "completion_conditions": spec["completion_conditions"],
                    "conditions_met": spec["conditions_met"],
                    "completed_at": spec["condition_met_at"] if completed else None,
                    "due_at": iso(spec["due_at"]),
                    "overdue": overdue and not completed,
                    "days_overdue": days if (overdue and not completed) else 0,
                    "escalation_level": level if not completed else 0,
                    "escalation_label": ESCALATION_LABELS.get(level) if not completed else None,
                    "reinspection_history": history,
                    "evidence_ids": spec["evidence_ids"],
                }
            )
        generated_ids.update(s["item_id"] for s in specs)

    orphaned = [r.to_dict() for r in reinspections if r.item_id not in generated_ids]
    return items, orphaned


def derive_escalations(remediation: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """汇总当前需要升级的整改项（逾期未完成）。"""
    result = []
    for item in remediation:
        if item["overdue"] and item["escalation_level"] >= 1:
            result.append(
                {
                    "item_id": item["item_id"],
                    "zone_id": item["zone_id"],
                    "kind": item["kind"],
                    "title": item["title"],
                    "escalation_level": item["escalation_level"],
                    "escalation_label": item["escalation_label"],
                    "days_overdue": item["days_overdue"],
                    "due_at": item["due_at"],
                }
            )
    return sorted(result, key=lambda e: (-e["escalation_level"], e["item_id"]))


def derive_all(
    incident_id: str,
    evidence: list[EvidenceRecord],
    reinspections: list[ReInspection],
    now: datetime,
) -> dict[str, Any]:
    zones = derive_impact_zones(evidence)
    phases = derive_phases(evidence, zones)
    remediation, orphaned = derive_remediation(incident_id, zones, evidence, reinspections, now)
    return {
        "impact_zones": zones,
        "phases": phases,
        "remediation": remediation,
        "escalations": derive_escalations(remediation),
        "orphaned_reinspections": orphaned,
    }
