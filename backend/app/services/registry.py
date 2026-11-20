"""Source registration: batches, baseline rule versions and contract versions."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..utils import INTERVAL_MINUTES, aware_utc, checksum_intervals


class RegistryError(ValueError):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def ingest_batch(db: Session, payload: dict, actor: str) -> models.MeterBatch:
    batch_key = (payload.get("batch_key") or "").strip()
    if not batch_key:
        raise RegistryError("batch_key 不能为空")
    existing = db.scalars(select(models.MeterBatch).where(models.MeterBatch.batch_key == batch_key)).first()
    if existing:
        return existing

    meter_no = (payload.get("meter_no") or "").strip()
    meter = db.scalars(select(models.MeterPoint).where(models.MeterPoint.meter_no == meter_no)).first()
    if meter is None:
        raise RegistryError(f"计量点不存在: {meter_no}", 404)

    raw_intervals = payload.get("intervals") or []
    if not raw_intervals:
        raise RegistryError("intervals 不能为空")
    intervals: list[dict] = []
    for item in raw_intervals:
        try:
            ts = aware_utc(datetime.fromisoformat(str(item["ts"]).replace("Z", "+00:00")))
            kwh = float(item["kwh"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RegistryError(f"区间数据格式错误: {item!r} ({exc})") from exc
        if kwh < 0:
            raise RegistryError("区间电量不能为负")
        intervals.append({"ts": ts, "kwh": kwh, "quality": item.get("quality", "good")})
    intervals.sort(key=lambda r: r["ts"])

    expected = int((intervals[-1]["ts"] - intervals[0]["ts"]).total_seconds() // (INTERVAL_MINUTES * 60)) + 1
    if len(intervals) != expected:
        raise RegistryError(f"区间不连续：期望 {expected} 个 15 分钟点，收到 {len(intervals)} 个")
    for prev, cur in zip(intervals, intervals[1:]):
        if (cur["ts"] - prev["ts"]).total_seconds() != INTERVAL_MINUTES * 60:
            raise RegistryError("区间存在缺口或重叠")

    supplied_start = payload.get("coverage_start")
    supplied_end = payload.get("coverage_end")
    coverage_start = (
        aware_utc(datetime.fromisoformat(supplied_start.replace("Z", "+00:00")))
        if supplied_start
        else intervals[0]["ts"]
    )
    coverage_end = (
        aware_utc(datetime.fromisoformat(supplied_end.replace("Z", "+00:00")))
        if supplied_end
        else intervals[-1]["ts"] + _delta()
    )
    if coverage_start != intervals[0]["ts"] or coverage_end != intervals[-1]["ts"] + _delta():
        raise RegistryError("coverage_start/coverage_end 必须与区间首尾一致（右开区间）")

    checksum = checksum_intervals(intervals)
    previous = db.scalars(
        select(models.MeterBatch)
        .where(models.MeterBatch.meter_point_id == meter.id)
        .order_by(models.MeterBatch.version.desc())
        .limit(1)
    ).first()
    if previous and previous.checksum == checksum:
        raise RegistryError(
            f"重复批次：内容与现有批次 #{previous.id}（v{previous.version}）一致，未登记", 409
        )

    version = (previous.version + 1) if previous else 1
    batch = models.MeterBatch(
        batch_key=batch_key,
        meter_point_id=meter.id,
        version=version,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        submitted_at=datetime.now(timezone.utc),
        checksum=checksum,
        supersedes_batch_id=previous.id if previous else None,
        source_note=payload.get("source_note", ""),
        status="accepted",
        interval_count=len(intervals),
        total_kwh=round(sum(r["kwh"] for r in intervals), 3),
        created_by=actor,
    )
    db.add(batch)
    db.flush()
    db.add_all(
        [
            models.MeterInterval(
                batch_id=batch.id,
                meter_point_id=meter.id,
                ts=r["ts"],
                kwh=r["kwh"],
                quality=r["quality"],
            )
            for r in intervals
        ]
    )
    db.commit()
    db.refresh(batch)
    return batch


def _delta():
    from datetime import timedelta

    return timedelta(minutes=INTERVAL_MINUTES)


def add_rule_version(db: Session, payload: dict, actor: str) -> models.BaselineRuleVersion:
    rule_code = payload.get("rule_code") or "BL-DEFAULT"
    reason = payload.get("reason") or "规则修订"
    latest = db.scalars(
        select(models.BaselineRuleVersion)
        .where(models.BaselineRuleVersion.rule_code == rule_code)
        .order_by(models.BaselineRuleVersion.version.desc())
        .limit(1)
    ).first()
    version = (latest.version + 1) if latest else 1
    rule = models.BaselineRuleVersion(
        rule_code=rule_code,
        version=version,
        name=payload.get("name") or f"基线规则 {rule_code} v{version}",
        method=payload.get("method", latest.method if latest else "same-interval-mean"),
        lookback_days=int(payload.get("lookback_days", latest.lookback_days if latest else 5)),
        min_sample_days=int(payload.get("min_sample_days", latest.min_sample_days if latest else 3)),
        adjustment_factor=float(payload.get("adjustment_factor", 1.0)),
        effective_from=_to_date(payload["effective_from"]),
        effective_to=None,
        reason=reason,
        config=payload.get("config") or {"description": "取事件前 N 个同窗口日均值，按调整系数折算"},
        created_by=actor,
    )
    db.add(rule)
    db.commit()
    db.refresh(rule)
    return rule


def add_contract_version(db: Session, payload: dict, actor: str) -> models.ContractVersion:
    customer_id = int(payload["customer_id"])
    customer = db.get(models.Customer, customer_id)
    if customer is None:
        raise RegistryError("客户不存在", 404)
    latest = db.scalars(
        select(models.ContractVersion)
        .where(models.ContractVersion.customer_id == customer_id)
        .order_by(models.ContractVersion.version.desc())
        .limit(1)
    ).first()
    version = (latest.version + 1) if latest else 1
    contract = models.ContractVersion(
        customer_id=customer_id,
        version=version,
        capacity_kw=float(payload["capacity_kw"]),
        price_ceiling=float(payload["price_ceiling"]) if payload.get("price_ceiling") is not None else None,
        effective_from=_to_date(payload["effective_from"]),
        reason=payload.get("reason") or "签约容量修订",
        created_by=actor,
    )
    db.add(contract)
    db.commit()
    db.refresh(contract)
    return contract


def _to_date(value) -> object:
    from datetime import date

    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def batch_fingerprint(rows: list[models.MeterInterval]) -> str:
    return hashlib.sha256(
        ";".join(f"{r.ts.isoformat()}|{float(r.kwh):.3f}" for r in rows).encode()
    ).hexdigest()
