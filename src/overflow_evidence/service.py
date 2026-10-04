"""溢流事件管理服务门面：台账、结论、整改、报告的统一入口。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .analysis import Conclusions, SlaConfig, derive_conclusions
from .importer import ImportResult, import_batch as _import_batch
from .ledger import (
    BackfillMeta,
    EvidenceLedger,
    EvidenceRecord,
    Withdrawal,
    parse_datetime,
)
from .remediation import RemediationRegistry
from .reports import Report, ReportStore, compute_digest, diff_snapshots

SCHEMA_VERSION = 1


class IncidentService:
    """把证据台账、结论推导、整改跟踪与报告签发组织成一个服务。"""

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] | None = None,
        backfill_threshold: timedelta = timedelta(hours=24),
        sla: SlaConfig | None = None,
        store_path: str | Path | None = None,
    ) -> None:
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.ledger = EvidenceLedger(backfill_threshold, self._clock)
        self.reports = ReportStore()
        self.remediations = RemediationRegistry()
        self.sla = sla or SlaConfig()
        self._descriptions: dict[str, str] = {}
        self._store_path = Path(store_path) if store_path else None
        if self._store_path and self._store_path.exists():
            self._load()

    # ---- 事件 ----
    def create_incident(self, incident_id: str, opened_at: Any, description: str = "") -> dict:
        self.ledger.open_incident(incident_id, opened_at)
        self._descriptions[incident_id] = description
        self._save()
        return {
            "incident_id": incident_id,
            "opened_at": parse_datetime(opened_at).isoformat(),
            "description": description,
        }

    def incidents(self) -> list[dict]:
        return [
            {
                "incident_id": iid,
                "opened_at": opened.isoformat(),
                "description": self._descriptions.get(iid, ""),
            }
            for iid, opened in sorted(self.ledger.incidents().items())
        ]

    # ---- 证据 ----
    def submit_evidence(
        self,
        incident_id: str,
        *,
        kind: str,
        observed_at: Any,
        location_code: str,
        payload: dict,
        evidence_id: str | None = None,
        source: str = "manual",
        recorded_at: Any = None,
        backfill_reason: str | None = None,
        confirmers: tuple[str, ...] | list[str] = (),
    ) -> EvidenceRecord:
        backfill = None
        if backfill_reason is not None or confirmers:
            backfill = BackfillMeta(reason=backfill_reason or "", confirmed_by=tuple(confirmers))
        rec = self.ledger.append(
            incident_id=incident_id,
            kind=kind,
            observed_at=observed_at,
            location_code=location_code,
            payload=payload,
            evidence_id=evidence_id,
            source=source,
            recorded_at=recorded_at,
            backfill=backfill,
        )
        self._save()
        return rec

    def import_batch(self, incident_id: str, records: list[Any]) -> ImportResult:
        result = _import_batch(self.ledger, incident_id, records)
        self._save()
        return result

    def withdraw_evidence(self, evidence_id: str, *, reason: str, withdrawn_by: str) -> Withdrawal:
        w = self.ledger.withdraw(evidence_id, reason=reason, withdrawn_by=withdrawn_by)
        self._save()
        return w

    def timeline(self, incident_id: str) -> list[dict]:
        out = []
        for r in self.ledger.timeline(incident_id):
            w = self.ledger.withdrawal_of(r.evidence_id)
            out.append({
                **r.to_dict(),
                "withdrawn": w is not None,
                "withdrawal": w.to_dict() if w else None,
            })
        return out

    def verify_chain(self) -> bool:
        return self.ledger.verify_chain()

    # ---- 结论 ----
    def conclusions(self, incident_id: str) -> Conclusions:
        """基于当前有效证据重新推导结论，并同步整改注册表。"""
        concl = derive_conclusions(self.ledger.records(incident_id), self.sla)
        self.remediations.sync(incident_id, concl.remediations)
        return concl

    # ---- 报告 ----
    def generate_report(self, incident_id: str) -> Report:
        concl = self.conclusions(incident_id)
        # 报告只反映当前派生出的整改项；已消失区段的历史任务仍留在注册表可查
        current_keys = {r.key for r in concl.remediations}
        tasks = [t for t in self.remediations.for_incident(incident_id)
                 if t.key in current_keys]
        snapshot = {
            "incident_id": incident_id,
            "schema": SCHEMA_VERSION,
            "evidence": {
                "active": [r.evidence_id for r in self.ledger.records(incident_id)],
                "withdrawn": [w.evidence_id for w in self.ledger.withdrawals(incident_id)],
            },
            "segments": {s.location_code: s.to_dict() for s in concl.segments},
            "remediations": {t.key: t.to_dict() for t in tasks},
        }
        version = self.reports.next_version(incident_id)
        report = Report(
            report_id=f"RPT-{incident_id}-{version}",
            incident_id=incident_id,
            version=version,
            generated_at=self._clock(),
            snapshot=snapshot,
            digest=compute_digest(snapshot),
        )
        self.reports.add(report)
        self._save()
        return report

    def get_report(self, report_id: str) -> Report:
        return self.reports.get(report_id)

    def list_reports(self, incident_id: str | None = None) -> list[dict]:
        return [r.summary() for r in self.reports.list(incident_id)]

    def verify_report(self, report_id: str) -> dict:
        report = self.reports.get(report_id)
        recomputed = compute_digest(report.snapshot)
        return {
            "report_id": report.report_id,
            "stored_digest": report.digest,
            "recomputed_digest": recomputed,
            "valid": recomputed == report.digest,
        }

    def diff_reports(self, old_report_id: str, new_report_id: str) -> dict:
        old = self.reports.get(old_report_id)
        new = self.reports.get(new_report_id)
        return {
            "from": old.report_id,
            "to": new.report_id,
            **diff_snapshots(old.snapshot, new.snapshot),
        }

    def conclusion_evidence(self, report_id: str, ref: str) -> list[dict]:
        """查询某份报告中任一结论所依据的证据材料。

        ref 形式：
        - report                整份报告依据的全部有效证据
        - segment:<区段>         影响区段结论
        - stage:<区段>:<阶段>    某区段的处置阶段结论
        - remediation:<key>     整改项结论（key 形如 contain:BLOCK-2）
        """
        report = self.reports.get(report_id)
        snap = report.snapshot
        if ref == "report":
            ids = snap["evidence"]["active"]
        elif ref.startswith("segment:"):
            loc = ref[len("segment:"):]
            ids = self._lookup(snap["segments"], loc, ref)["evidence_ids"]
        elif ref.startswith("stage:"):
            _, loc, stage = ref.split(":", 2)
            seg = self._lookup(snap["segments"], loc, ref)
            matches = [s for s in seg["stages"] if s["stage"] == stage]
            if not matches:
                raise KeyError(f"报告 {report_id} 中不存在结论 {ref}")
            ids = matches[0]["evidence_ids"]
        elif ref.startswith("remediation:"):
            key = ref[len("remediation:"):]
            ids = self._lookup(snap["remediations"], key, ref)["evidence_ids"]
        else:
            raise KeyError(f"无法识别的结论引用: {ref}")
        return [self.ledger.get(eid).to_dict() for eid in ids]

    @staticmethod
    def _lookup(mapping: dict, key: str, ref: str) -> dict:
        if key not in mapping:
            raise KeyError(f"报告中不存在结论 {ref}")
        return mapping[key]

    # ---- 整改 ----
    def remediation_completion(self, task_id: str) -> dict:
        return self.remediations.completion(task_id, self._clock())

    def remediations_for(self, incident_id: str) -> list[dict]:
        self.conclusions(incident_id)  # 先同步派生项
        return [self.remediations.completion(t.task_id, self._clock())
                for t in self.remediations.for_incident(incident_id)]

    def record_reinspection(
        self,
        task_id: str,
        *,
        evidence_id: str,
        passed: bool,
        inspector: str,
        at: Any = None,
        note: str = "",
    ) -> dict:
        self.ledger.get(evidence_id)  # 复验依据必须真实存在
        rec = self.remediations.record_reinspection(
            task_id, evidence_id=evidence_id, passed=passed,
            inspector=inspector, at=at, note=note,
        )
        self._save()
        return rec.to_dict()

    def check_overdue(self, now: Any = None) -> list[dict]:
        out = self.remediations.check_overdue(now if now is not None else self._clock())
        self._save()
        return out

    # ---- 持久化 ----
    def _save(self) -> None:
        if not self._store_path:
            return
        data = {
            "schema": SCHEMA_VERSION,
            "descriptions": self._descriptions,
            "ledger": self.ledger.dump(),
            "reports": self.reports.dump(),
            "remediations": self.remediations.dump(),
        }
        tmp = self._store_path.with_suffix(self._store_path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self._store_path)

    def _load(self) -> None:
        data = json.loads(self._store_path.read_text(encoding="utf-8"))
        self._descriptions = dict(data.get("descriptions", {}))
        self.ledger.load(data.get("ledger", {}))
        self.reports.load(data.get("reports", {}))
        self.remediations.load(data.get("remediations", {}))
