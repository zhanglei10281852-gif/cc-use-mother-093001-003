"""报告签发、可校验摘要与历次报告差异。

报告是某一时刻结论的不可变快照：内容哈希（digest）覆盖全部输入证据
哈希与结论，并与上一版报告哈希衔接成链；签发后可通过 ``verify_report``
重新计算摘要以校验完整性，``diff_reports`` 对比任意两版结论差异。
"""
from __future__ import annotations

from typing import Any

from .models import IncidentState
from .store import GENESIS_HASH, canonical_json, iso, sha256_text


def build_report(
    *,
    incident_id: str,
    version: int,
    generated_at,
    generated_by: str,
    state: IncidentState,
    derived: dict[str, Any],
    prev_digest: str,
) -> dict[str, Any]:
    included = state.included_evidence()
    excluded = state.excluded_evidence()
    body: dict[str, Any] = {
        "report_id": f"RPT-{incident_id}-{version:03d}",
        "incident_id": incident_id,
        "version": version,
        "generated_at": iso(generated_at),
        "generated_by": generated_by,
        "evidence_inputs": [
            {
                "evidence_id": r.evidence_id,
                "content_hash": r.content_hash,
                "kind": r.kind,
                "location_code": r.location_code,
                "observed_at": iso(r.observed_at),
            }
            for r in included
        ],
        "excluded_evidence": [
            {
                "evidence_id": r.evidence_id,
                "status": r.status,
                "reason": (
                    "awaiting_dual_confirmation"
                    if r.status == "pending"
                    else (r.withdrawn or {}).get("reason", "withdrawn")
                ),
            }
            for r in excluded
        ],
        "impact_zones": derived["impact_zones"],
        "phases": derived["phases"],
        "remediation": derived["remediation"],
        "escalations": derived["escalations"],
        "orphaned_reinspections": derived["orphaned_reinspections"],
        "summary": {
            "evidence_count": len(included),
            "excluded_count": len(excluded),
            "zones_active": sum(1 for z in derived["impact_zones"] if z["status"] == "active"),
            "zones_cleared": sum(1 for z in derived["impact_zones"] if z["status"] == "cleared"),
            "remediation_completed": sum(1 for i in derived["remediation"] if i["status"] == "completed"),
            "remediation_open": sum(1 for i in derived["remediation"] if i["status"] != "completed"),
            "escalation_count": len(derived["escalations"]),
        },
        "prev_digest": prev_digest or GENESIS_HASH,
    }
    body["digest"] = report_digest(body)
    return body


def report_digest(report_body: dict[str, Any]) -> str:
    """对报告全部内容（除 digest 自身）计算可校验摘要。"""
    body = {k: v for k, v in report_body.items() if k != "digest"}
    return sha256_text(canonical_json(body))


def verify_report(report: dict[str, Any], states: dict[str, IncidentState]) -> dict[str, Any]:
    """校验报告：摘要自洽、输入证据哈希与日志一致、前版链接存在。"""
    checks: list[dict[str, Any]] = []
    recomputed = report_digest(report)
    checks.append(
        {"name": "digest_self_consistent", "ok": recomputed == report.get("digest"), "detail": recomputed}
    )

    state = states.get(report["incident_id"])
    for entry in report.get("evidence_inputs", []):
        record = state.evidence.get(entry["evidence_id"]) if state else None
        # 报告是签发时刻的快照：只校验内容哈希与日志一致，不要求证据当前
        # 仍处于已确认状态（签发后撤回不影响旧报告的有效性）。
        ok = record is not None and record.content_hash == entry["content_hash"]
        checks.append(
            {
                "name": "evidence_input_intact",
                "ok": ok,
                "detail": entry["evidence_id"],
            }
        )

    prev = report.get("prev_digest", GENESIS_HASH)
    if prev == GENESIS_HASH:
        checks.append({"name": "prev_link", "ok": True, "detail": "genesis"})
    else:
        linked = bool(state) and any(r["digest"] == prev for r in state.reports)
        checks.append({"name": "prev_link", "ok": linked, "detail": prev})

    return {"report_id": report["report_id"], "valid": all(c["ok"] for c in checks), "checks": checks}


def _diff_dicts(old: dict[str, Any], new: dict[str, Any], fields: list[str]) -> dict[str, Any]:
    changes = {}
    for field_name in fields:
        if old.get(field_name) != new.get(field_name):
            changes[field_name] = {"from": old.get(field_name), "to": new.get(field_name)}
    return changes


def diff_reports(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """对比两版报告：证据集合、区段、阶段、整改项与升级的变化。"""
    old_inputs = {e["evidence_id"] for e in old.get("evidence_inputs", [])}
    new_inputs = {e["evidence_id"] for e in new.get("evidence_inputs", [])}

    def keyed(report, section, key):
        return {item[key]: item for item in report.get(section, [])}

    def section_diff(section, key, fields):
        old_items, new_items = keyed(old, section, key), keyed(new, section, key)
        added = sorted(k for k in new_items if k not in old_items)
        removed = sorted(k for k in old_items if k not in new_items)
        changed = []
        for item_key in sorted(old_items.keys() & new_items.keys()):
            changes = _diff_dicts(old_items[item_key], new_items[item_key], fields)
            if changes:
                changed.append({key: item_key, "changes": changes})
        return {"added": added, "removed": removed, "changed": changed}

    old_esc = {e["item_id"] for e in old.get("escalations", [])}
    new_esc = {e["item_id"] for e in new.get("escalations", [])}

    return {
        "from": {"report_id": old["report_id"], "version": old["version"]},
        "to": {"report_id": new["report_id"], "version": new["version"]},
        "evidence": {
            "added": sorted(new_inputs - old_inputs),
            "removed": sorted(old_inputs - new_inputs),
        },
        "impact_zones": section_diff(
            "impact_zones",
            "zone_id",
            ["status", "severity", "score", "first_detected_at", "last_positive_at", "cleared_at", "evidence_ids"],
        ),
        "phases": section_diff("phases", "name", ["status", "started_at", "completed_at", "evidence_ids"]),
        "remediation": section_diff(
            "remediation",
            "item_id",
            ["status", "conditions_met", "completed_at", "overdue", "escalation_level", "blocked_by"],
        ),
        "escalations": {"raised": sorted(new_esc - old_esc), "resolved": sorted(old_esc - new_esc)},
        "summary": _diff_dicts(
            old.get("summary", {}),
            new.get("summary", {}),
            ["evidence_count", "zones_active", "zones_cleared", "remediation_completed", "remediation_open", "escalation_count"],
        ),
    }
