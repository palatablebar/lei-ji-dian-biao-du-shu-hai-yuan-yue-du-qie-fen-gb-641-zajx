"""采信边界的纯函数：窗口归属、业务键、入场计划。

不碰存储；给定已存在的台账键集合与当前边界，算出本次运行的处置方案。
"""
from __future__ import annotations

import datetime as dt
import hashlib

from ... import config

# 处置类别
KIND_NEW = "new"            # 边界之后的新采信
KIND_BACKFILL = "backfill"  # 边界之前的迟到补采（仍入账）
KIND_DUPLICATE = "duplicate"  # 台账已有，重复执行同一采集段，跳过


def window_date_of(read_at_utc: str) -> str:
    """UTC 时间 → 本地自然日（窗口 id）。"""
    value = dt.datetime.fromisoformat(read_at_utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=config.UTC)
    return value.astimezone(config.CHINA_TZ).date().isoformat()


def next_day(window_date: str) -> str:
    day = dt.date.fromisoformat(window_date) + dt.timedelta(days=1)
    return day.isoformat()


def dedupe_key(meter_id: str, register: str, read_at_utc: str) -> str:
    """业务键：与 DATA-DICTIONARY 第 6 节同一个 (表号, 寄存器, UTC 时间)。"""
    return f"{meter_id}|{register}|{read_at_utc}"


def quarantine_key(item: dict) -> str:
    """隔离证据键：按内容哈希，重跑同一文件不会重复入账。"""
    parts = [item.get("code") or "", item.get("stage") or "",
             item.get("meter_id") or "", item.get("field") or "",
             item.get("raw_value") or "", item.get("detail") or ""]
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:24]


def plan_admission(records: list[dict], quarantine_items: list[dict], *,
                   ledger_existing: set[str], quarantine_existing: set[str],
                   boundary_before: str | None) -> dict:
    """计算一次运行的采信处置。

    records：本批有效 + 估抄记录（含 read_at，UTC ISO）。
    quarantine_items：本批隔离行，能归属窗口的已带 read_at（否则为 None）。
    返回的 window_deltas 只含「新见到的键」，重复执行同一采集段时全部为零。
    """
    boundary = boundary_before
    admissions: list[dict] = []
    ledger_new: list[dict] = []
    quarantine_new: list[dict] = []
    deltas: dict[str, dict[str, int]] = {}
    seen_in_run: set[str] = set()
    max_window: str | None = None

    def delta(window_date: str) -> dict[str, int]:
        nonlocal max_window
        if max_window is None or window_date > max_window:
            max_window = window_date
        return deltas.setdefault(window_date, {
            "seen": 0, "admitted": 0, "quarantined": 0, "backfilled": 0})

    for record in records:
        key = dedupe_key(record["meter_id"], record["register"], record["read_at"])
        window_date = window_date_of(record["read_at"])
        if key in ledger_existing or key in seen_in_run:
            kind = KIND_DUPLICATE
        else:
            kind = (KIND_BACKFILL
                    if boundary is not None and window_date < boundary
                    else KIND_NEW)
            seen_in_run.add(key)
            ledger_new.append({"dedupe_key": key, "meter_id": record["meter_id"],
                               "register": record["register"],
                               "read_at": record["read_at"],
                               "window_date": window_date, "kind": kind})
            bucket = delta(window_date)
            bucket["seen"] += 1
            bucket["admitted"] += 1
            if kind == KIND_BACKFILL:
                bucket["backfilled"] += 1
        admissions.append({"dedupe_key": key, "meter_id": record["meter_id"],
                           "register": record["register"],
                           "read_at": record["read_at"],
                           "window_date": window_date, "kind": kind})

    for item in quarantine_items:
        key = quarantine_key(item)
        if key in quarantine_existing:
            continue
        quarantine_existing.add(key)  # 同一份文件里的相同坏行只记一次
        row = {"quarantine_key": key, "window_date": None,
               "code": item["code"], "stage": item["stage"],
               "level": item["level"], "meter_id": item.get("meter_id"),
               "field": item.get("field"), "raw_value": item.get("raw_value"),
               "detail": item.get("detail")}
        read_at = item.get("read_at")
        if read_at:
            row["window_date"] = window_date_of(read_at)
            bucket = delta(row["window_date"])
            bucket["seen"] += 1
            bucket["quarantined"] += 1
        quarantine_new.append(row)

    boundary_after = boundary
    if max_window is not None:
        candidate = next_day(max_window)
        if boundary_after is None or candidate > boundary_after:
            boundary_after = candidate

    return {
        "boundary_before": boundary,
        "boundary_after": boundary_after,
        "admissions": admissions,
        "ledger_new": ledger_new,
        "quarantine_new": quarantine_new,
        "window_deltas": deltas,
        "stats": {
            "admitted_new": sum(1 for a in admissions if a["kind"] == KIND_NEW),
            "backfill": sum(1 for a in admissions if a["kind"] == KIND_BACKFILL),
            "duplicates": sum(1 for a in admissions if a["kind"] == KIND_DUPLICATE),
            "quarantined_new": len(quarantine_new),
        },
    }
