"""命令行入口：登记证据、签发报告、查询结论依据与历次差异。

用法示例：
  python -m overflow_evidence.cli --db data.json create-incident INC-1 --opened-at 2026-09-29T08:00:00+08:00
  python -m overflow_evidence.cli --db data.json submit INC-1 --kind sensor \
      --location BLOCK-2 --observed-at 2026-09-29T09:00:00+08:00 \
      --payload '{"metric":"cod","value":120,"limit":40}'
  python -m overflow_evidence.cli --db data.json report INC-1
  python -m overflow_evidence.cli --db data.json diff RPT-INC-1-1 RPT-INC-1-2
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .ledger import LedgerError, parse_datetime
from .remediation import RemediationError
from .reports import ReportError
from .service import IncidentService

KINDS = ("sensor", "inspection", "sample", "work_receipt")


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="overflow-cli", description="溢流事件证据与整改闭环管理")
    p.add_argument("--db", default=os.environ.get("OVERFLOW_DB", "overflow_db.json"),
                   help="数据文件路径（默认 overflow_db.json，可用 OVERFLOW_DB 覆盖）")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("create-incident", help="建立事件")
    sp.add_argument("incident_id")
    sp.add_argument("--opened-at", required=True, help="事件建立时间（ISO，需带时区）")
    sp.add_argument("--description", default="")

    sp = sub.add_parser("incidents", help="事件列表")

    sp = sub.add_parser("submit", help="登记证据（晚录自动要求补录理由与双人确认）")
    sp.add_argument("incident_id")
    sp.add_argument("--kind", required=True, choices=KINDS)
    sp.add_argument("--location", required=True)
    sp.add_argument("--observed-at", required=True, help="事件发生时间（ISO，需带时区）")
    sp.add_argument("--payload", required=True, help="JSON 对象，字段随类型而定")
    sp.add_argument("--evidence-id")
    sp.add_argument("--source", default="manual")
    sp.add_argument("--backfill-reason", help="补录理由")
    sp.add_argument("--confirm", action="append", default=[], dest="confirmers",
                    help="补录确认人，需恰好两名不同人员（重复传入此参数）")

    sp = sub.add_parser("import", help="批量导入（坏记录进入隔离区）")
    sp.add_argument("incident_id")
    sp.add_argument("--file", required=True, help="JSON 文件：记录数组或 {\"records\": [...]}")

    sp = sub.add_parser("withdraw", help="撤回证据（原始记录保留，结论重算）")
    sp.add_argument("evidence_id")
    sp.add_argument("--reason", required=True)
    sp.add_argument("--by", required=True, dest="withdrawn_by")

    sp = sub.add_parser("timeline", help="可信时间线")
    sp.add_argument("incident_id")

    sp = sub.add_parser("conclusions", help="当前结论（实时推导）")
    sp.add_argument("incident_id")

    sp = sub.add_parser("report", help="签发新报告（生成可校验摘要）")
    sp.add_argument("incident_id")

    sp = sub.add_parser("reports", help="报告列表")
    sp.add_argument("incident_id", nargs="?")

    sp = sub.add_parser("show", help="报告全文")
    sp.add_argument("report_id")

    sp = sub.add_parser("verify", help="校验报告摘要")
    sp.add_argument("report_id")

    sp = sub.add_parser("diff", help="两版报告差异")
    sp.add_argument("old")
    sp.add_argument("new")

    sp = sub.add_parser("evidence-of", help="查询结论所依据的材料")
    sp.add_argument("report_id")
    sp.add_argument("ref", help="report | segment:<区段> | stage:<区段>:<阶段> | remediation:<key>")

    sp = sub.add_parser("remediations", help="整改完成条件列表")
    sp.add_argument("incident_id")

    sp = sub.add_parser("completion", help="单个整改项的完成条件")
    sp.add_argument("task_id")

    sp = sub.add_parser("reinspect", help="登记复验结果")
    sp.add_argument("task_id")
    sp.add_argument("--evidence", required=True, help="复验依据的证据编号")
    sp.add_argument("--passed", required=True, choices=("yes", "no"))
    sp.add_argument("--by", required=True, dest="inspector")
    sp.add_argument("--note", default="")

    sp = sub.add_parser("escalate", help="逾期升级检查")
    sp.add_argument("--now", help="检查时点（ISO，默认当前时间）")

    sp = sub.add_parser("verify-chain", help="校验台账哈希链")

    sp = sub.add_parser("serve", help="启动 HTTP 接口")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8080)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    svc = IncidentService(store_path=args.db)
    try:
        if args.cmd == "create-incident":
            _print(svc.create_incident(args.incident_id, args.opened_at, args.description))
        elif args.cmd == "incidents":
            _print(svc.incidents())
        elif args.cmd == "submit":
            payload = json.loads(args.payload)
            rec = svc.submit_evidence(
                args.incident_id,
                kind=args.kind,
                observed_at=args.observed_at,
                location_code=args.location,
                payload=payload,
                evidence_id=args.evidence_id,
                source=args.source,
                backfill_reason=args.backfill_reason,
                confirmers=args.confirmers,
            )
            _print(rec.to_dict())
        elif args.cmd == "import":
            data = json.loads(Path(args.file).read_text(encoding="utf-8"))
            records = data["records"] if isinstance(data, dict) else data
            _print(svc.import_batch(args.incident_id, records).to_dict())
        elif args.cmd == "withdraw":
            _print(svc.withdraw_evidence(
                args.evidence_id, reason=args.reason, withdrawn_by=args.withdrawn_by).to_dict())
        elif args.cmd == "timeline":
            _print(svc.timeline(args.incident_id))
        elif args.cmd == "conclusions":
            _print(svc.conclusions(args.incident_id).to_dict())
        elif args.cmd == "report":
            _print(svc.generate_report(args.incident_id).summary())
        elif args.cmd == "reports":
            _print(svc.list_reports(args.incident_id))
        elif args.cmd == "show":
            _print(svc.get_report(args.report_id).to_dict())
        elif args.cmd == "verify":
            _print(svc.verify_report(args.report_id))
        elif args.cmd == "diff":
            _print(svc.diff_reports(args.old, args.new))
        elif args.cmd == "evidence-of":
            _print(svc.conclusion_evidence(args.report_id, args.ref))
        elif args.cmd == "remediations":
            _print(svc.remediations_for(args.incident_id))
        elif args.cmd == "completion":
            _print(svc.remediation_completion(args.task_id))
        elif args.cmd == "reinspect":
            _print(svc.record_reinspection(
                args.task_id,
                evidence_id=args.evidence,
                passed=args.passed == "yes",
                inspector=args.inspector,
                note=args.note,
            ))
        elif args.cmd == "escalate":
            _print(svc.check_overdue(args.now))
        elif args.cmd == "verify-chain":
            _print({"valid": svc.verify_chain()})
        elif args.cmd == "serve":
            from .api import serve
            serve(svc, args.host, args.port)
        return 0
    except (LedgerError, RemediationError, ReportError, KeyError, ValueError,
            FileNotFoundError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
