"""命令行接口：证据登记、确认、撤回、导入、报告与查询。

用法示例：
    python -m overflow_evidence.cli --data-dir ./data init-incident --incident-id INC-1 --title 中秋冒溢
    python -m overflow_evidence.cli --data-dir ./data add-evidence --incident-id INC-1 ...
    python -m overflow_evidence.cli --data-dir ./data issue-report --incident-id INC-1 --by 复盘组
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from overflow_evidence.errors import OverflowError
from overflow_evidence.service import OverflowService


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _service(args) -> OverflowService:
    return OverflowService(args.data_dir)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="overflow-evidence", description="雨污溢流证据与整改闭环管理")
    parser.add_argument(
        "--data-dir",
        default=os.environ.get("OVERFLOW_DATA_DIR", ".overflow_data"),
        help="数据目录（默认 .overflow_data，可用 OVERFLOW_DATA_DIR 覆盖）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init-incident", help="登记溢流事件")
    p.add_argument("--incident-id", required=True)
    p.add_argument("--title", required=True)
    p.add_argument("--description", default="")
    p.add_argument("--by", default="system")

    p = sub.add_parser("add-evidence", help="登记证据（传感/巡查/样本/作业回执）")
    p.add_argument("--incident-id", required=True)
    p.add_argument("--kind", required=True, choices=["sensor", "inspection", "sample", "work_receipt"])
    p.add_argument("--observed-at", required=True, help="ISO8601，须含时区")
    p.add_argument("--location", required=True)
    p.add_argument("--source", required=True, help="提供单位，如 排水公司/环卫/施工单位")
    p.add_argument("--by", required=True, help="登记人")
    p.add_argument("--payload", required=True, help="JSON 字符串或 @文件路径")
    p.add_argument("--evidence-id", default=None)
    p.add_argument("--is-backfill", action="store_true")
    p.add_argument("--backfill-reason", default=None)

    p = sub.add_parser("confirm", help="补录证据双人确认")
    p.add_argument("--evidence-id", required=True)
    p.add_argument("--by", required=True, help="确认人（须与登记人及另一确认人不同）")

    p = sub.add_parser("withdraw", help="撤回证据（保留记录，结论重算）")
    p.add_argument("--evidence-id", required=True)
    p.add_argument("--reason", required=True)
    p.add_argument("--by", required=True)

    p = sub.add_parser("show-evidence", help="查看证据详情")
    p.add_argument("--evidence-id", required=True)

    p = sub.add_parser("import", help="批量导入（坏记录隔离，不污染已确认数据）")
    p.add_argument("--incident-id", required=True)
    p.add_argument("--file", required=True, help="JSON 数组文件")
    p.add_argument("--mode", choices=["partial", "atomic"], default="partial")
    p.add_argument("--by", default="batch-import")

    p = sub.add_parser("timeline", help="可信时间线（含哈希链）")
    p.add_argument("--incident-id", required=True)

    p = sub.add_parser("conclusions", help="当前结论：影响区段/处置阶段/整改项/升级")
    p.add_argument("--incident-id", required=True)

    p = sub.add_parser("issue-report", help="签发报告（生成可校验摘要）")
    p.add_argument("--incident-id", required=True)
    p.add_argument("--by", required=True)

    p = sub.add_parser("list-reports", help="历次报告")
    p.add_argument("--incident-id", required=True)

    p = sub.add_parser("show-report", help="报告全文")
    p.add_argument("--report-id", required=True)

    p = sub.add_parser("verify-report", help="校验报告摘要与证据链")
    p.add_argument("--report-id", required=True)

    p = sub.add_parser("diff-reports", help="两版报告差异")
    p.add_argument("--incident-id", required=True)
    p.add_argument("--a", required=True)
    p.add_argument("--b", required=True)

    p = sub.add_parser("provenance", help="查询结论所依据的原始材料")
    p.add_argument("--report-id", required=True)
    p.add_argument("--ref", required=True, help="如 zone:ZONE-BLOCK-2 / phase:remediation / remediation:REM-...")

    p = sub.add_parser("remediation", help="整改项与完成条件")
    p.add_argument("--incident-id", required=True)
    p.add_argument("--item", default=None, help="整改项编号（缺省列出全部）")

    p = sub.add_parser("reinspect", help="登记复验结果")
    p.add_argument("--incident-id", required=True)
    p.add_argument("--item", required=True)
    p.add_argument("--result", required=True, choices=["pass", "fail"])
    p.add_argument("--by", required=True)
    p.add_argument("--note", default="")
    p.add_argument("--evidence-id", default=None, help="作为复验依据的已确认证据")

    p = sub.add_parser("escalations", help="逾期升级清单")
    p.add_argument("--incident-id", required=True)

    p = sub.add_parser("verify-chain", help="校验事件日志哈希链")

    p = sub.add_parser("serve", help="启动 HTTP 接口")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8080)
    return parser


def _load_payload(raw: str):
    if raw.startswith("@"):
        raw = Path(raw[1:]).read_text(encoding="utf-8")
    return json.loads(raw)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    service = _service(args)
    try:
        if args.command == "init-incident":
            _print(service.create_incident(args.incident_id, args.title, args.description, actor=args.by))
        elif args.command == "add-evidence":
            _print(
                service.register_evidence(
                    args.incident_id,
                    kind=args.kind,
                    observed_at=args.observed_at,
                    location_code=args.location,
                    payload=_load_payload(args.payload),
                    source=args.source,
                    registered_by=args.by,
                    evidence_id=args.evidence_id,
                    is_backfill=args.is_backfill,
                    backfill_reason=args.backfill_reason,
                )
            )
        elif args.command == "confirm":
            _print(service.confirm_evidence(args.evidence_id, args.by))
        elif args.command == "withdraw":
            _print(service.withdraw_evidence(args.evidence_id, args.reason, args.by))
        elif args.command == "show-evidence":
            _print(service.get_evidence(args.evidence_id))
        elif args.command == "import":
            records = json.loads(Path(args.file).read_text(encoding="utf-8"))
            _print(service.import_batch(args.incident_id, records, mode=args.mode, actor=args.by))
        elif args.command == "timeline":
            _print(service.timeline(args.incident_id))
        elif args.command == "conclusions":
            _print(service.conclusions(args.incident_id))
        elif args.command == "issue-report":
            _print(service.issue_report(args.incident_id, args.by))
        elif args.command == "list-reports":
            _print(service.list_reports(args.incident_id))
        elif args.command == "show-report":
            _print(service.get_report(args.report_id))
        elif args.command == "verify-report":
            _print(service.verify_report(args.report_id))
        elif args.command == "diff-reports":
            _print(service.diff_reports(args.incident_id, args.a, args.b))
        elif args.command == "provenance":
            _print(service.conclusion_evidence(args.report_id, args.ref))
        elif args.command == "remediation":
            _print(service.remediation(args.incident_id, args.item))
        elif args.command == "reinspect":
            _print(
                service.record_reinspection(
                    args.incident_id,
                    args.item,
                    result=args.result,
                    inspector=args.by,
                    note=args.note,
                    evidence_id=args.evidence_id,
                )
            )
        elif args.command == "escalations":
            _print(service.escalations(args.incident_id))
        elif args.command == "verify-chain":
            _print(service.verify_chain())
        elif args.command == "serve":
            from overflow_evidence.api import serve

            serve(service, host=args.host, port=args.port)
        else:  # pragma: no cover
            raise OverflowError(f"未知命令：{args.command}")
    except OverflowError as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
