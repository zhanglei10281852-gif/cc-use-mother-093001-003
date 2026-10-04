"""测试共享工具：固定时钟与中秋冒溢场景。"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from overflow_evidence.service import OverflowService

NOW = datetime(2026, 9, 29, 12, 0, 0, tzinfo=timezone.utc)
INCIDENT = "INC-2026-0929"


class Clock:
    def __init__(self, now: datetime = NOW):
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now = self.now + timedelta(**kwargs)


def make_service(data_dir, clock: Clock | None = None) -> OverflowService:
    return OverflowService(data_dir, clock=clock or Clock())


def build_midautumn_scenario(service: OverflowService) -> dict[str, str]:
    """中秋客流高峰冒溢：传感超标 → 巡查确认 → 样本超标 → 封路 → 清掏 → 修复 → 复查恢复。"""
    service.create_incident(INCIDENT, "中秋客流高峰下水道冒溢", actor="街区管理方")
    ids = {}

    def add(key, **kwargs):
        record = service.register_evidence(INCIDENT, evidence_id=key, **kwargs)
        ids[key] = record["evidence_id"]

    add(
        "E-S1", kind="sensor", observed_at="2026-09-29T08:00:00+00:00", location_code="BLOCK-2",
        payload={"parameter": "cod", "value": 45.0, "threshold": 30.0}, source="排水公司", registered_by="传感平台",
    )
    add(
        "E-I1", kind="inspection", observed_at="2026-09-29T08:30:00+00:00", location_code="BLOCK-2",
        payload={"overflow_observed": True, "photo_refs": ["P-101"]}, source="环卫", registered_by="巡查员甲",
    )
    add(
        "E-L1", kind="sample", observed_at="2026-09-29T09:00:00+00:00", location_code="BLOCK-2",
        payload={"parameter": "cod", "value": 90.0, "limit": 30.0, "lab_id": "LAB-7"}, source="排水公司", registered_by="化验员乙",
    )
    add(
        "E-W1", kind="work_receipt", observed_at="2026-09-29T09:30:00+00:00", location_code="BLOCK-2",
        payload={"action": "road_closure", "crew": "施工一队"}, source="施工单位", registered_by="施工员丙",
    )
    add(
        "E-W2", kind="work_receipt", observed_at="2026-09-29T10:00:00+00:00", location_code="BLOCK-2",
        payload={"action": "cleaning", "crew": "施工一队"}, source="施工单位", registered_by="施工员丙",
    )
    add(
        "E-W3", kind="work_receipt", observed_at="2026-09-29T11:00:00+00:00", location_code="BLOCK-2",
        payload={"action": "repair", "crew": "施工二队"}, source="施工单位", registered_by="施工员丁",
    )
    add(
        "E-I2", kind="inspection", observed_at="2026-09-29T11:30:00+00:00", location_code="BLOCK-2",
        payload={"overflow_observed": False, "photo_refs": ["P-118"]}, source="环卫", registered_by="巡查员甲",
    )
    return ids
