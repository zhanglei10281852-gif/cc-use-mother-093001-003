"""溢流事件管理服务：证据归档、结论推导、整改跟踪与报告签发。"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from . import derive, importer, reports
from .contracts import EvidenceKind
from .errors import ConflictError, NotFoundError, ValidationError
from .models import STATUS_CONFIRMED, STATUS_PENDING, STATUS_WITHDRAWN, IncidentState, replay
from .store import EventStore, canonical_json, iso, parse_ts, sha256_text, utcnow

DEFAULT_BACKFILL_THRESHOLD = timedelta(hours=24)

KIND_LABELS = {
    "sensor": "传感读数",
    "inspection": "人工巡查",
    "sample": "实验室样本",
    "work_receipt": "作业回执",
}


class OverflowService:
    """服务门面：命令行与 HTTP 接口共用。"""

    def __init__(
        self,
        data_dir: str | Path,
        clock: Callable[[], datetime] | None = None,
        backfill_threshold: timedelta | None = None,
    ):
        self.store = EventStore(data_dir)
        self.clock = clock or utcnow
        self.backfill_threshold = backfill_threshold or DEFAULT_BACKFILL_THRESHOLD

    # ------------------------------------------------------------------ state
    def _load(self) -> tuple[dict[str, IncidentState], dict[str, str]]:
        return replay(self.store.events())

    def _require_incident(self, states: dict[str, IncidentState], incident_id: str) -> IncidentState:
        state = states.get(incident_id)
        if state is None or state.info is None:
            raise NotFoundError(f"事件不存在：{incident_id}")
        return state

    def _require_evidence(self, states, evidence_index, evidence_id: str):
        incident_id = evidence_index.get(evidence_id)
        if incident_id is None:
            raise NotFoundError(f"证据不存在：{evidence_id}")
        return states[incident_id].evidence[evidence_id]

    def _derived(self, state: IncidentState) -> dict[str, Any]:
        return derive.derive_all(
            state.incident_id, state.included_evidence(), state.reinspections, self.clock()
        )

    # --------------------------------------------------------------- incidents
    def create_incident(self, incident_id: str, title: str, description: str = "", actor: str = "system") -> dict[str, Any]:
        if not incident_id or not title:
            raise ValidationError("incident_id 与 title 必填")
        states, _ = self._load()
        if incident_id in states and states[incident_id].info is not None:
            raise ConflictError(f"事件已存在：{incident_id}")
        self.store.append(
            "incident_created", incident_id, actor, {"title": title, "description": description}, recorded_at=self.clock()
        )
        return self.get_incident(incident_id)

    def get_incident(self, incident_id: str) -> dict[str, Any]:
        states, _ = self._load()
        state = self._require_incident(states, incident_id)
        return {**state.info, "evidence_total": len(state.order), "reports": list(state.reports)}

    # ---------------------------------------------------------------- evidence
    def register_evidence(
        self,
        incident_id: str,
        *,
        kind: str,
        observed_at: str,
        location_code: str,
        payload: dict[str, Any],
        source: str,
        registered_by: str,
        evidence_id: str | None = None,
        is_backfill: bool = False,
        backfill_reason: str | None = None,
    ) -> dict[str, Any]:
        states, evidence_index = self._load()
        self._require_incident(states, incident_id)

        if kind not in {k.value for k in EvidenceKind}:
            raise ValidationError(f"未知证据类型：{kind!r}")
        errors = importer.validate_payload(kind, payload)
        if errors:
            raise ValidationError("; ".join(errors))
        if not location_code or not source or not registered_by:
            raise ValidationError("location_code、source、registered_by 必填")
        try:
            observed = parse_ts(observed_at)
        except (ValueError, TypeError) as exc:
            raise ValidationError(f"observed_at 非法：{exc}") from exc

        now = self.clock()
        if observed > now + timedelta(minutes=5):
            raise ValidationError("observed_at 晚于当前时间，疑似伪造")

        backfill = bool(is_backfill) or (now - observed > self.backfill_threshold)
        if backfill and not backfill_reason:
            raise ValidationError("补录证据必须填写 backfill_reason")

        evidence_id = evidence_id or f"E-{uuid.uuid4().hex[:12]}"
        if evidence_id in evidence_index:
            raise ConflictError(f"证据编号已存在：{evidence_id}")

        content_hash = sha256_text(
            canonical_json(
                {
                    "evidence_id": evidence_id,
                    "incident_id": incident_id,
                    "kind": kind,
                    "observed_at": iso(observed),
                    "location_code": location_code,
                    "payload": payload,
                    "source": source,
                    "registered_by": registered_by,
                }
            )
        )
        self.store.append(
            "evidence_registered",
            incident_id,
            registered_by,
            {
                "evidence_id": evidence_id,
                "kind": kind,
                "observed_at": iso(observed),
                "location_code": location_code,
                "payload": payload,
                "source": source,
                "registered_by": registered_by,
                "registered_at": iso(now),
                "is_backfill": backfill,
                "backfill_reason": backfill_reason,
                "content_hash": content_hash,
                "initial_status": STATUS_PENDING if backfill else STATUS_CONFIRMED,
            },
            recorded_at=now,
        )
        states, _ = self._load()
        return states[incident_id].evidence[evidence_id].to_dict()

    def confirm_evidence(self, evidence_id: str, confirmer: str) -> dict[str, Any]:
        """补录证据的双人确认：确认人须互不相同且不能是登记人。"""
        if not confirmer:
            raise ValidationError("confirmer 必填")
        states, evidence_index = self._load()
        record = self._require_evidence(states, evidence_index, evidence_id)
        if record.status == STATUS_WITHDRAWN:
            raise ConflictError(f"证据已撤回，无法确认：{evidence_id}")
        if record.status == STATUS_CONFIRMED:
            raise ConflictError(f"证据已确认，无需重复确认：{evidence_id}")
        if confirmer == record.registered_by:
            raise ValidationError("确认人不能与登记人相同")
        if any(c["confirmer"] == confirmer for c in record.confirmations):
            raise ConflictError(f"{confirmer} 已确认过该证据")
        confirmation_no = len(record.confirmations) + 1
        self.store.append(
            "evidence_confirmed",
            record.incident_id,
            confirmer,
            {"evidence_id": evidence_id, "confirmer": confirmer, "confirmation_no": confirmation_no},
            recorded_at=self.clock(),
        )
        states, _ = self._load()
        return states[record.incident_id].evidence[evidence_id].to_dict()

    def withdraw_evidence(self, evidence_id: str, reason: str, withdrawn_by: str) -> dict[str, Any]:
        """撤回证据：追加撤回事件，原记录保留，结论随下次推导重算。"""
        if not reason:
            raise ValidationError("撤回必须填写 reason")
        if not withdrawn_by:
            raise ValidationError("withdrawn_by 必填")
        states, evidence_index = self._load()
        record = self._require_evidence(states, evidence_index, evidence_id)
        if record.status == STATUS_WITHDRAWN:
            raise ConflictError(f"证据已撤回：{evidence_id}")
        self.store.append(
            "evidence_withdrawn",
            record.incident_id,
            withdrawn_by,
            {"evidence_id": evidence_id, "reason": reason, "previous_status": record.status},
            recorded_at=self.clock(),
        )
        states, _ = self._load()
        return states[record.incident_id].evidence[evidence_id].to_dict()

    def get_evidence(self, evidence_id: str) -> dict[str, Any]:
        states, evidence_index = self._load()
        return self._require_evidence(states, evidence_index, evidence_id).to_dict()

    # ------------------------------------------------------------------ import
    def import_batch(
        self,
        incident_id: str,
        records: list[Any],
        *,
        mode: str = "partial",
        actor: str = "batch-import",
    ) -> dict[str, Any]:
        """批量导入。partial：逐条校验，坏记录隔离；atomic：任一坏记录则整批拒绝。"""
        if mode not in ("partial", "atomic"):
            raise ValidationError("mode 必须是 partial 或 atomic")
        states, evidence_index = self._load()
        self._require_incident(states, incident_id)
        if not isinstance(records, list):
            raise ValidationError("records 必须是数组")

        now = self.clock()
        batch_id = f"IMP-{uuid.uuid4().hex[:12]}"
        existing_ids = set(evidence_index)
        seen: set[str] = set()
        validated: list[tuple[dict[str, Any], list[str]]] = []
        for record in records:
            errors = importer.validate_record(
                record,
                existing_ids=existing_ids,
                seen_in_batch=seen,
                now=now,
                backfill_threshold=self.backfill_threshold,
            )
            if isinstance(record, dict) and record.get("evidence_id"):
                seen.add(record["evidence_id"])
            validated.append((record, errors))

        rejected = [
            {"index": idx, "record": record, "errors": errors}
            for idx, (record, errors) in enumerate(validated)
            if errors
        ]
        if rejected and mode == "atomic":
            for entry in rejected:
                self.store.quarantine(
                    {"batch_id": batch_id, "incident_id": incident_id, "mode": mode, "at": iso(now), **entry}
                )
            return {
                "batch_id": batch_id,
                "mode": mode,
                "imported": 0,
                "quarantined": len(rejected),
                "rejected": rejected,
                "committed": False,
            }

        imported_ids: list[str] = []
        for record, errors in validated:
            if errors:
                self.store.quarantine(
                    {
                        "batch_id": batch_id,
                        "incident_id": incident_id,
                        "mode": mode,
                        "at": iso(now),
                        "record": record,
                        "errors": errors,
                    }
                )
                continue
            registered = self.register_evidence(
                incident_id,
                kind=record["kind"],
                observed_at=record["observed_at"],
                location_code=record["location_code"],
                payload=record["payload"],
                source=record["source"],
                registered_by=record["registered_by"],
                evidence_id=record.get("evidence_id"),
                is_backfill=bool(record.get("is_backfill")),
                backfill_reason=record.get("backfill_reason"),
            )
            imported_ids.append(registered["evidence_id"])
            if registered["status"] == STATUS_PENDING:
                # 补录记录可随批次附带双人确认；确认人合法性已在校验阶段检查
                for confirmer in record.get("confirmations", [])[:2]:
                    self.confirm_evidence(registered["evidence_id"], confirmer)

        return {
            "batch_id": batch_id,
            "mode": mode,
            "imported": len(imported_ids),
            "imported_ids": imported_ids,
            "quarantined": len(rejected),
            "rejected": rejected,
            "committed": True,
        }

    # --------------------------------------------------------------- timeline
    def timeline(self, incident_id: str) -> dict[str, Any]:
        states, _ = self._load()
        self._require_incident(states, incident_id)
        entries = []
        for event in self.store.events(incident_id):
            entries.append(
                {
                    "seq": event["seq"],
                    "event_id": event["event_id"],
                    "type": event["type"],
                    "actor": event["actor"],
                    "recorded_at": event["recorded_at"],
                    "hash": event["hash"],
                    "prev_hash": event["prev_hash"],
                    "summary": _summarize(event),
                }
            )
        return {"incident_id": incident_id, "events": entries}

    # ------------------------------------------------------------- conclusions
    def conclusions(self, incident_id: str) -> dict[str, Any]:
        """当前结论（不落库）：影响区段、处置阶段、整改项与升级。"""
        states, _ = self._load()
        state = self._require_incident(states, incident_id)
        derived = self._derived(state)
        return {"incident_id": incident_id, "computed_at": iso(self.clock()), **derived}

    # ----------------------------------------------------------------- reports
    def issue_report(self, incident_id: str, generated_by: str) -> dict[str, Any]:
        if not generated_by:
            raise ValidationError("generated_by 必填")
        states, _ = self._load()
        state = self._require_incident(states, incident_id)
        version = len(state.reports) + 1
        prev_digest = state.reports[-1]["digest"] if state.reports else ""
        report = reports.build_report(
            incident_id=incident_id,
            version=version,
            generated_at=self.clock(),
            generated_by=generated_by,
            state=state,
            derived=self._derived(state),
            prev_digest=prev_digest,
        )
        self.store.save_report(report)
        self.store.append(
            "report_issued",
            incident_id,
            generated_by,
            {"report_id": report["report_id"], "version": version, "digest": report["digest"]},
            recorded_at=self.clock(),
        )
        return report

    def get_report(self, report_id: str) -> dict[str, Any]:
        report = self.store.load_report(report_id)
        if report is None:
            raise NotFoundError(f"报告不存在：{report_id}")
        return report

    def list_reports(self, incident_id: str) -> list[dict[str, Any]]:
        states, _ = self._load()
        state = self._require_incident(states, incident_id)
        return list(state.reports)

    def verify_report(self, report_id: str) -> dict[str, Any]:
        report = self.get_report(report_id)
        states, _ = self._load()
        return reports.verify_report(report, states)

    def diff_reports(self, incident_id: str, report_a: str, report_b: str) -> dict[str, Any]:
        old, new = self.get_report(report_a), self.get_report(report_b)
        for report in (old, new):
            if report["incident_id"] != incident_id:
                raise ValidationError(f"报告 {report['report_id']} 不属于事件 {incident_id}")
        if old["version"] > new["version"]:
            old, new = new, old
        return reports.diff_reports(old, new)

    def conclusion_evidence(self, report_id: str, ref: str) -> dict[str, Any]:
        """查询任一结论（区段/阶段/整改项）所依据的原始材料。"""
        report = self.get_report(report_id)
        if ":" not in ref:
            raise ValidationError("ref 形如 zone:ZONE-X、phase:detection、remediation:REM-...")
        kind, _, key = ref.partition(":")
        section_map = {"zone": "impact_zones", "phase": "phases", "remediation": "remediation"}
        section = section_map.get(kind)
        if section is None:
            raise ValidationError(f"未知结论类型：{kind}（可选 zone/phase/remediation）")
        key_field = {"impact_zones": "zone_id", "phases": "name", "remediation": "item_id"}[section]
        conclusion = next((c for c in report[section] if c.get(key_field) == key), None)
        if conclusion is None:
            raise NotFoundError(f"报告中不存在结论：{ref}")
        states, _ = self._load()
        state = states.get(report["incident_id"])
        materials = []
        for evidence_id in conclusion.get("evidence_ids", []):
            record = state.evidence.get(evidence_id) if state else None
            if record is not None:
                materials.append(record.to_dict())
        return {"report_id": report_id, "ref": ref, "conclusion": conclusion, "materials": materials}

    # -------------------------------------------------------------- remediation
    def remediation(self, incident_id: str, item_id: str | None = None) -> Any:
        conclusions = self.conclusions(incident_id)
        if item_id is None:
            return conclusions["remediation"]
        for item in conclusions["remediation"]:
            if item["item_id"] == item_id:
                return item
        raise NotFoundError(f"整改项不存在：{item_id}")

    def record_reinspection(
        self,
        incident_id: str,
        item_id: str,
        *,
        result: str,
        inspector: str,
        note: str = "",
        evidence_id: str | None = None,
    ) -> dict[str, Any]:
        if result not in ("pass", "fail"):
            raise ValidationError("result 必须是 pass 或 fail")
        if not inspector:
            raise ValidationError("inspector 必填")
        self.remediation(incident_id, item_id)  # 不存在则抛 404
        if evidence_id is not None:
            record = self.get_evidence(evidence_id)
            if record["status"] != STATUS_CONFIRMED:
                raise ValidationError("复验依据的证据必须是已确认状态")
        self.store.append(
            "reinspection_recorded",
            incident_id,
            inspector,
            {
                "item_id": item_id,
                "result": result,
                "inspector": inspector,
                "note": note,
                "evidence_id": evidence_id,
            },
            recorded_at=self.clock(),
        )
        return self.remediation(incident_id, item_id)

    def escalations(self, incident_id: str) -> list[dict[str, Any]]:
        return self.conclusions(incident_id)["escalations"]

    # ------------------------------------------------------------------ chain
    def verify_chain(self) -> dict[str, Any]:
        return self.store.verify_chain()


def _summarize(event: dict[str, Any]) -> str:
    etype, data = event["type"], event["data"]
    if etype == "incident_created":
        return f"事件登记：{data.get('title', '')}"
    if etype == "evidence_registered":
        label = KIND_LABELS.get(data.get("kind"), data.get("kind", ""))
        suffix = "（补录）" if data.get("is_backfill") else ""
        return f"登记{label} {data['evidence_id']}@{data['location_code']}{suffix}"
    if etype == "evidence_confirmed":
        return f"证据 {data['evidence_id']} 获 {data['confirmer']} 确认（第 {data.get('confirmation_no')} 人）"
    if etype == "evidence_withdrawn":
        return f"证据 {data['evidence_id']} 被撤回：{data.get('reason', '')}"
    if etype == "reinspection_recorded":
        return f"整改项 {data['item_id']} 复验{'通过' if data['result'] == 'pass' else '未通过'}"
    if etype == "report_issued":
        return f"签发报告 {data['report_id']}（第 {data['version']} 版）"
    return etype
