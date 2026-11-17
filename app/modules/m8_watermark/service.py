"""采信边界编排：把纯函数和存储接起来，给批次流程与页面供数。"""
from __future__ import annotations

import datetime as dt

from ... import errors
from . import windows as win


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def build_plan(store, records: list[dict], quarantine_items: list[dict]) -> dict:
    """查询台账现状，算出本批处置方案（供 save_run 同事务落库）。"""
    state = store.load_watermark()
    boundary = state["boundary"] if state else None
    keys = [win.dedupe_key(r["meter_id"], r["register"], r["read_at"])
            for r in records]
    q_keys = [win.quarantine_key(item) for item in quarantine_items]
    return win.plan_admission(
        records, quarantine_items,
        ledger_existing=store.ledger_membership(keys),
        quarantine_existing=store.quarantine_membership(q_keys),
        boundary_before=boundary)


def run_payload(store, plan: dict, batch_id: str) -> dict:
    """运行响应里的 watermark 段：本次处置 + 推进后的未闭合窗口。"""
    touched = []
    status_by_date = {w["window_date"]: w["status"] for w in store.list_windows()}
    for window_date, delta in sorted(plan.get("window_deltas", {}).items()):
        touched.append({"window_date": window_date,
                        "admitted": delta["admitted"],
                        "backfilled": delta["backfilled"],
                        "quarantined": delta["quarantined"],
                        "status": status_by_date.get(window_date, "open")})
    return {
        "batch_id": batch_id,
        "boundary_before": plan.get("boundary_before"),
        "boundary_after": plan.get("boundary_after"),
        **plan["stats"],
        "windows_touched": touched,
        "unclosed": _unclosed(store),
    }


def replay_payload(store, batch_id: str) -> dict:
    """幂等重放：不触碰边界，只报告现状与该批的处置记录。"""
    state = store.load_watermark()
    admissions = store.load_admissions(batch_id)
    return {
        "batch_id": batch_id,
        "boundary_before": state["boundary"] if state else None,
        "boundary_after": state["boundary"] if state else None,
        "admitted_new": sum(1 for a in admissions if a["kind"] == win.KIND_NEW),
        "backfill": sum(1 for a in admissions if a["kind"] == win.KIND_BACKFILL),
        "duplicates": sum(1 for a in admissions
                          if a["kind"] == win.KIND_DUPLICATE),
        "quarantined_new": 0,
        "windows_touched": [],
        "unclosed": _unclosed(store),
    }


def payload(store) -> dict:
    """GET /api/watermark 与处理页面共用的现状视图。"""
    state = store.load_watermark()
    windows = store.list_windows()
    return {
        "boundary": state["boundary"] if state else None,
        "updated_at": state["updated_at"] if state else None,
        "batch_id": state["batch_id"] if state else None,
        "windows": windows,
        "unclosed": [w for w in windows if w["status"] == "unclosed"],
        "counts": {
            "windows": len(windows),
            "open": sum(1 for w in windows if w["status"] == "open"),
            "closed": sum(1 for w in windows if w["status"] == "closed"),
            "unclosed": sum(1 for w in windows if w["status"] == "unclosed"),
            "ledger": store.count_ledger(),
        },
    }


def advance(store, date_text: str) -> dict:
    """显式推进边界（每天必须推进，即使当天没有文件到达）。只进不退。"""
    try:
        day = dt.date.fromisoformat(str(date_text).strip())
    except ValueError as exc:
        raise errors.BadRequest(f"边界日期无法解析：{date_text!r}",
                                code="E_WATERMARK_INVALID") from exc
    result = store.advance_watermark(
        boundary=day.isoformat(), batch_id="manual",
        updated_at=_now_iso())
    result.update(payload(store))
    return result


def _unclosed(store) -> list[dict]:
    return [{
        "window_date": w["window_date"], "seen": w["seen"],
        "quarantined": w["quarantined"], "first_batch": w["first_batch"],
        "last_batch": w["last_batch"],
    } for w in store.list_windows(status="unclosed")]
