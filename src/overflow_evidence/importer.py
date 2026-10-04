"""批量导入的记录级校验。

每条记录独立校验，错误汇总返回；坏记录进入隔离区（quarantine），
不会写入主事件日志，因而不会污染已确认数据。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .contracts import EvidenceKind
from .derive import CLEANING_ACTIONS, CONTAINMENT_ACTIONS, REMEDIATION_ACTIONS
from .store import parse_ts

WORK_RECEIPT_ACTIONS = CONTAINMENT_ACTIONS | REMEDIATION_ACTIONS

REQUIRED_FIELDS = ("kind", "observed_at", "location_code", "payload", "source", "registered_by")


def validate_payload(kind: str, payload: Any) -> list[str]:
    """按证据类型校验载荷结构，返回错误列表（空为通过）。"""
    errors: list[str] = []
    if not isinstance(payload, dict):
        return ["payload 必须是对象"]
    if kind == EvidenceKind.SENSOR.value:
        if not isinstance(payload.get("parameter"), str) or not payload.get("parameter"):
            errors.append("sensor.payload.parameter 必填")
        if not isinstance(payload.get("value"), (int, float)):
            errors.append("sensor.payload.value 必须是数值")
        if "threshold" in payload and not isinstance(payload["threshold"], (int, float)):
            errors.append("sensor.payload.threshold 必须是数值")
    elif kind == EvidenceKind.SAMPLE.value:
        if not isinstance(payload.get("parameter"), str) or not payload.get("parameter"):
            errors.append("sample.payload.parameter 必填")
        if not isinstance(payload.get("value"), (int, float)):
            errors.append("sample.payload.value 必须是数值")
        if not isinstance(payload.get("limit"), (int, float)) or payload.get("limit", 0) <= 0:
            errors.append("sample.payload.limit 必须是正数")
    elif kind == EvidenceKind.INSPECTION.value:
        if not isinstance(payload.get("overflow_observed"), bool):
            errors.append("inspection.payload.overflow_observed 必须是布尔值")
    elif kind == EvidenceKind.WORK_RECEIPT.value:
        if payload.get("action") not in WORK_RECEIPT_ACTIONS:
            errors.append(f"work_receipt.payload.action 必须是 {sorted(WORK_RECEIPT_ACTIONS)} 之一")
    else:
        errors.append(f"未知证据类型：{kind!r}")
    return errors


def validate_record(
    record: Any,
    *,
    existing_ids: set[str],
    seen_in_batch: set[str],
    now: datetime,
    backfill_threshold: timedelta,
    future_tolerance: timedelta = timedelta(minutes=5),
) -> list[str]:
    """校验单条导入记录，返回错误列表（空为可导入）。"""
    if not isinstance(record, dict):
        return ["记录必须是对象"]
    errors: list[str] = []
    for field_name in REQUIRED_FIELDS:
        if record.get(field_name) in (None, ""):
            errors.append(f"缺少必填字段：{field_name}")

    kind = record.get("kind")
    if kind is not None and kind not in {k.value for k in EvidenceKind}:
        errors.append(f"未知证据类型：{kind!r}")

    evidence_id = record.get("evidence_id")
    if evidence_id:
        if evidence_id in existing_ids:
            errors.append(f"证据编号已存在：{evidence_id}")
        if evidence_id in seen_in_batch:
            errors.append(f"批次内编号重复：{evidence_id}")

    observed_at = None
    raw_observed = record.get("observed_at")
    if raw_observed:
        try:
            observed_at = parse_ts(raw_observed)
        except (ValueError, TypeError):
            errors.append("observed_at 不是合法的 ISO8601 时间或缺少时区")
    if observed_at is not None and observed_at > now + future_tolerance:
        errors.append("observed_at 晚于当前时间，疑似伪造")

    if isinstance(record.get("payload"), dict) or "payload" in record:
        if isinstance(kind, str):
            errors.extend(validate_payload(kind, record.get("payload")))

    is_backfill = bool(record.get("is_backfill"))
    if not is_backfill and observed_at is not None and now - observed_at > backfill_threshold:
        is_backfill = True
    if is_backfill:
        if not record.get("backfill_reason"):
            errors.append("补录记录必须填写 backfill_reason")
        confirmations = record.get("confirmations", [])
        if confirmations:
            if not isinstance(confirmations, list) or any(
                not isinstance(c, str) or not c for c in confirmations
            ):
                errors.append("confirmations 必须是非空字符串列表")
            else:
                if len(confirmations) != len(set(confirmations)):
                    errors.append("confirmations 中存在重复确认人")
                if record.get("registered_by") in confirmations:
                    errors.append("确认人不能与登记人相同")
    return errors
