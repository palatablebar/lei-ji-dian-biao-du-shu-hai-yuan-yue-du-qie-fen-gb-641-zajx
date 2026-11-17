"""M8 采信边界：可续跑、迟到补采、幂等、失败保留旧边界、隔离留存。

覆盖的不变量：
- 成功运行后边界推进到「本批最大窗口 + 1 天」，只进不退；
- 迟到数据补进更早窗口：仍采信、记 backfill、缺数窗口随之闭合；
- 重复执行同一采集段：台账命中，新增为零，窗口计数不变；
- 边界切换失败：上一份可用边界原样保留（同事务回滚）；
- 更早窗口的隔离记录跨批次留存，不随新批次消失；
- 重启（新建 Store）后边界与窗口台账仍在，可续跑。
"""
from __future__ import annotations

import pytest

from app import errors
from app.modules.m6_batches import service as batch_service
from app.modules.m8_watermark import service as watermark_service
from app.modules.m8_watermark import windows as win
from app.store import Store

DIRTY = "readings_dirty.csv"
LATE = "readings_late.csv"


def _run(settings, store, sample=DIRTY, mode="full"):
    return batch_service.run(settings, store, sample=sample, mode=mode)


@pytest.fixture()
def dirty_run(settings, store):
    return _run(settings, store, DIRTY)


# ---------- 边界推进与窗口状态 ----------

def test_boundary_advances_to_day_after_max_window(dirty_run, store):
    state = store.load_watermark()
    assert state is not None
    assert state["boundary"] == "2026-04-17"  # 最大窗口 2026-04-16 + 1 天
    assert state["batch_id"] == dirty_run["batch"]["batch_id"]


def test_windows_with_only_quarantined_rows_stay_unclosed(dirty_run, store):
    unclosed = {w["window_date"] for w in store.list_windows(status="unclosed")}
    # 2026-02-12 换表后抄表、2026-02-16 超满量：窗口出现过但零采信
    assert unclosed == {"2026-02-12", "2026-02-16"}
    closed = {w["window_date"] for w in store.list_windows(status="closed")}
    assert "2026-02-15" in closed  # 同窗口既有采信也有隔离 → 已闭合


def test_boundary_survives_restart(dirty_run, settings):
    reopened = Store(settings.db_path)  # 模拟重启：同一库文件重新打开
    state = reopened.load_watermark()
    assert state["boundary"] == "2026-04-17"
    unclosed = {w["window_date"] for w in reopened.list_windows(status="unclosed")}
    assert unclosed == {"2026-02-12", "2026-02-16"}


# ---------- 迟到补采 ----------

def test_late_backfill_closes_unclosed_windows(settings, store):
    _run(settings, store, DIRTY)
    result = _run(settings, store, LATE)
    watermark = result["watermark"]
    assert watermark["boundary_before"] == "2026-04-17"
    assert watermark["boundary_after"] == "2026-04-17"  # 补采不把边界拖回
    assert watermark["backfill"] == 5
    assert watermark["admitted_new"] == 0
    assert watermark["duplicates"] == 1  # M0001 2026-03-01 重复上送
    assert watermark["unclosed"] == []   # 两个缺数窗口都被补采闭合
    window_0216 = next(w for w in store.list_windows()
                       if w["window_date"] == "2026-02-16")
    assert window_0216["status"] == "closed"
    assert window_0216["admitted"] == 2
    assert window_0216["backfilled"] == 2
    assert window_0216["quarantined"] == 2  # 超满量（脏样例）+ 迟到的负数行


def test_backfill_traceable_to_batch(settings, store):
    first = _run(settings, store, DIRTY)
    second = _run(settings, store, LATE)
    admissions = store.load_admissions(second["batch"]["batch_id"])
    backfill = [a for a in admissions if a["kind"] == "backfill"]
    assert {a["window_date"] for a in backfill} == {
        "2026-01-16", "2026-01-20", "2026-02-12", "2026-02-16"}
    # 台账能区分首次采信批次：补采键不属于第一个批次
    with store.connect() as conn:
        rows = conn.execute(
            "SELECT kind, first_batch FROM reading_ledger WHERE window_date=?",
            ("2026-02-12",)).fetchall()
    assert rows and all(r["first_batch"] == second["batch"]["batch_id"]
                        for r in rows)
    assert first["batch"]["batch_id"] != second["batch"]["batch_id"]


# ---------- 幂等：重复执行同一采集段 ----------

def test_replay_same_segment_adds_nothing(settings, store):
    _run(settings, store, DIRTY)
    before = store.count_ledger()
    replay = _run(settings, store, DIRTY)  # 命中批次键，整批复用
    assert replay["idempotent_replay"] is True
    assert store.count_ledger() == before

    forced = _run(settings, store, DIRTY, mode="force")  # 强制执行同一采集段
    watermark = forced["watermark"]
    assert watermark["admitted_new"] == 0
    assert watermark["backfill"] == 0
    assert watermark["duplicates"] == 46
    assert watermark["quarantined_new"] == 0
    assert store.count_ledger() == before  # 台账一行未增
    assert store.load_watermark()["boundary"] == "2026-04-17"


def test_window_counters_not_inflated_by_rerun(settings, store):
    _run(settings, store, DIRTY)
    before = {w["window_date"]: dict(w) for w in store.list_windows()}
    _run(settings, store, DIRTY, mode="force")
    after = {w["window_date"]: dict(w) for w in store.list_windows()}
    for date, row in before.items():
        for key in ("seen", "admitted", "quarantined", "backfilled", "status"):
            assert after[date][key] == row[key], (date, key)


# ---------- 边界切换失败保留上一份可用边界 ----------

def test_failed_switch_keeps_previous_boundary(settings, store):
    _run(settings, store, DIRTY)
    before = store.load_watermark()
    ledger_before = store.count_ledger()
    plan = win.plan_admission(
        [{"meter_id": "M0001", "register": "TOTAL",
          "read_at": "2026-05-01T00:00:00+00:00"}],
        [], ledger_existing=set(), quarantine_existing=set(),
        boundary_before=before["boundary"])
    assert plan["boundary_after"] == "2026-05-02"
    batch = {"batch_id": "mb-broken", "batch_key": "k", "run_id": "r",
             "sample": "x", "status": "done", "created_at": "t",
             "finished_at": "t", "input_digest": "d", "rule_version": "v",
             "updated_at": "t"}
    with pytest.raises(Exception):
        store.save_run(
            batch=batch, meters=[], readings=[], intervals=[], allocations=[],
            bills=[{"month": "2026-05"}],  # 缺列 → 事务中途失败
            quarantine=[], stages=[], totals={}, manifest_json={},
            watermark={**plan, "batch_id": "mb-broken", "updated_at": "t"})
    assert store.load_watermark() == before       # 旧边界原样保留
    assert store.count_ledger() == ledger_before  # 台账也没有半截写入


def test_failed_run_never_touches_boundary(settings, store):
    _run(settings, store, DIRTY)
    before = store.load_watermark()
    with pytest.raises(errors.MeterbackError):
        _run(settings, store, "readings_empty.csv")  # 文件级失败
    assert store.load_watermark() == before


# ---------- 更早窗口的隔离记录不消失 ----------

def test_quarantine_ledger_survives_later_batches(settings, store):
    _run(settings, store, DIRTY)
    ledger_before = store.load_quarantine_ledger()
    assert len(ledger_before) == 14
    keyed = {(q["window_date"], q["code"]) for q in ledger_before}
    assert ("2026-02-16", "Q_EXCEEDS_CAPACITY") in keyed

    _run(settings, store, "readings_clean.csv")  # 新批次覆盖另一批窗口
    ledger_after = store.load_quarantine_ledger()
    assert len(ledger_after) >= len(ledger_before)
    for row in ledger_before:
        assert row in ledger_after  # 旧窗口的隔离证据一条不少


def test_earlier_window_quarantine_visible_after_boundary(settings, store):
    _run(settings, store, DIRTY)
    boundary = store.load_watermark()["boundary"]
    earlier = store.load_quarantine_ledger(before=boundary)
    assert earlier  # 边界之前的窗口隔离记录始终查得到
    assert all(q["window_date"] < boundary for q in earlier)


# ---------- 显式推进（每天必须推进，即使当天无文件） ----------

def test_manual_advance_only_moves_forward(settings, store):
    _run(settings, store, DIRTY)
    backwards = watermark_service.advance(store, "2026-04-01")
    assert backwards["advanced"] is False
    assert backwards["boundary"] == "2026-04-17"

    forwards = watermark_service.advance(store, "2026-05-01")
    assert forwards["advanced"] is True
    assert store.load_watermark()["boundary"] == "2026-05-01"
    # 推进后所有已登记窗口都在边界之内，状态重算闭合/未闭合
    assert not store.list_windows(status="open")


def test_manual_advance_rejects_bad_date(settings, store):
    _run(settings, store, DIRTY)
    with pytest.raises(errors.MeterbackError):
        watermark_service.advance(store, "2026-02-30")


# ---------- 接口与页面 ----------

def test_api_watermark_and_windows(client_with_run):
    response = client_with_run.get("/api/watermark")
    assert response.status_code == 200
    body = response.json()
    assert body["boundary"] == "2026-04-17"
    assert body["counts"]["unclosed"] == 2
    assert {w["window_date"] for w in body["unclosed"]} == {
        "2026-02-12", "2026-02-16"}

    unclosed = client_with_run.get("/api/watermark/windows?status=unclosed")
    assert unclosed.status_code == 200
    assert len(unclosed.json()["windows"]) == 2

    advance = client_with_run.post("/api/watermark/advance",
                                   json={"date": "2026-05-01"})
    assert advance.status_code == 200
    assert advance.json()["advanced"] is True

    bad = client_with_run.post("/api/watermark/advance", json={"date": "not-a-date"})
    assert bad.status_code == 400
    assert bad.json()["error"] == "E_WATERMARK_INVALID"


def test_run_response_shows_admissions_and_unclosed(client):
    response = client.post("/api/runs", json={"sample": DIRTY})
    assert response.status_code == 200
    watermark = response.json()["watermark"]
    assert watermark["admitted_new"] == 46
    assert watermark["boundary_after"] == "2026-04-17"
    assert len(watermark["unclosed"]) == 2

    page = client.get("/readings")
    assert page.status_code == 200
    assert "采信边界" in page.text
    assert "2026-02-12" in page.text  # 未闭合窗口列在页面上
    assert "本次处置" in page.text    # 本次纳入明细
