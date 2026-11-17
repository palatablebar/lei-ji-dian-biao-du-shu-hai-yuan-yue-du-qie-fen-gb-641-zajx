"""M8 入口：看采信边界与窗口台账、显式推进边界。"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from ...web import Ctx, get_ctx
from . import service

router = APIRouter(tags=["M8 采信边界"])


@router.get("/api/watermark")
def api_watermark(ctx: Ctx = Depends(get_ctx)) -> dict:
    return service.payload(ctx.store)


@router.get("/api/watermark/windows")
def api_windows(status: str | None = None, ctx: Ctx = Depends(get_ctx)) -> dict:
    return {"windows": ctx.store.list_windows(status=status)}


@router.post("/api/watermark/advance")
def api_advance(payload: dict | None = None, ctx: Ctx = Depends(get_ctx)) -> dict:
    body = payload or {}
    return service.advance(ctx.store, str(body.get("date") or ""))
