"""命令行冒烟：端到端走一遍 证据登记 → 结论 → 报告 → 撤回 → 差异 → 校验。

运行：python run_cli.py
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from overflow_evidence.service import IncidentService

CST = timezone(timedelta(hours=8))
T0 = datetime(2026, 9, 29, 8, 0, tzinfo=CST)


def main() -> None:
    # 固定时钟，让冒烟在任意日期运行结果一致
    svc = IncidentService(clock=lambda: T0 + timedelta(hours=10))
    svc.create_incident("INC-2026-MID", T0, "中秋客流高峰下水道冒溢")

    # 现场证据：传感超标、巡查确认、封控与清掏回执
    svc.submit_evidence("INC-2026-MID", kind="sensor", observed_at=T0 + timedelta(hours=1),
                        location_code="BLOCK-2",
                        payload={"metric": "cod", "value": 120.0, "limit": 40.0})
    svc.submit_evidence("INC-2026-MID", kind="inspection", observed_at=T0 + timedelta(hours=1, minutes=30),
                        location_code="BLOCK-2",
                        payload={"overflow_observed": True, "notes": "井口冒溢"})
    svc.submit_evidence("INC-2026-MID", kind="work_receipt", observed_at=T0 + timedelta(hours=2),
                        location_code="BLOCK-2",
                        payload={"action": "sealing", "completed": True})
    svc.submit_evidence("INC-2026-MID", kind="work_receipt", observed_at=T0 + timedelta(hours=5),
                        location_code="BLOCK-2",
                        payload={"action": "cleaning", "completed": True})

    # 批量导入：一条坏记录（缺 limit）进隔离区，不污染已确认数据
    imp = svc.import_batch("INC-2026-MID", [
        {"kind": "sample", "observed_at": (T0 + timedelta(hours=30)).isoformat(),
         "location_code": "BLOCK-2",
         "payload": {"analyte": "cod", "value": 25.0, "limit": 40.0}},
        {"kind": "sample", "observed_at": (T0 + timedelta(hours=31)).isoformat(),
         "location_code": "BLOCK-2",
         "payload": {"analyte": "cod", "value": 25.0}},
    ])

    r1 = svc.generate_report("INC-2026-MID")

    # 撤回巡查记录后重新签发：旧报告保留，差异可查
    svc.withdraw_evidence("EV-00002", reason="照片编号与现场不符，待补拍", withdrawn_by="值班长")
    r2 = svc.generate_report("INC-2026-MID")

    print(json.dumps({
        "import": imp.to_dict(),
        "report_v1": r1.summary(),
        "report_v2": r2.summary(),
        "verify_v1": svc.verify_report(r1.report_id)["valid"],
        "verify_v2": svc.verify_report(r2.report_id)["valid"],
        "diff": svc.diff_reports(r1.report_id, r2.report_id),
        "chain_valid": svc.verify_chain(),
        "completion_contain": svc.remediation_completion("INC-2026-MID:contain:BLOCK-2")["status"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
