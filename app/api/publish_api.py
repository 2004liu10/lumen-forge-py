"""发布记录 —— 对应 `server/index.js:1698-1794`。

记录的是「哪个视频文件、什么时候填过标题话题、什么时候真的发出去了」。
前端「发布台」据此显示每个文件的进度，并支持「哪些已经可以删了」。
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from ..publish_log import (
    analyze_deletable,
    check_publish,
    clear_records,
    list_publish_records,
    list_publish_works,
    mark_deleted,
    mark_filled,
    mark_published,
    mark_published_by_identity,
    remove_record,
    unmark_published,
)
from ..store import store
from .deps import as_int, bad_request, json_body, not_found, ok, str_or

router = APIRouter()


def _save() -> None:
    store.mark_dirty("publish_log")
    store.save()
    store.flush()


@router.get("/api/publish/records")
async def get_records() -> dict:
    return {"records": list_publish_records()}


@router.get("/api/publish/works")
async def get_works() -> dict:
    out = list_publish_works()
    return {"works": out.get("works") or [], "hidden": out.get("hidden") or []}


@router.post("/api/publish/check")
async def post_check(request: Request) -> dict:
    body = await json_body(request)
    return check_publish(str_or(body.get("path")))


@router.post("/api/publish/filled")
async def post_filled(request: Request) -> dict:
    body = await json_body(request)
    record = mark_filled(
        path=str_or(body.get("path")),
        name=body.get("name"),
        desc=str_or(body.get("desc")),
        topics=body.get("topics"),
    )
    _save()
    return {"ok": True, "record": record, "records": list_publish_records()}


@router.post("/api/publish/records/{record_id}/published")
async def post_published(record_id: str) -> dict:
    mark_published(record_id)
    _save()
    return {"ok": True, "records": list_publish_records()}


@router.post("/api/publish/published-by-path")
async def post_published_by_path(request: Request) -> dict:
    body = await json_body(request)
    record = mark_published_by_identity(str_or(body.get("path")))
    _save()
    return {"ok": True, "record": record, "records": list_publish_records()}


@router.post("/api/publish/records/{record_id}/unpublish")
async def post_unpublish(record_id: str) -> dict:
    unmark_published(record_id)
    _save()
    return {"ok": True, "records": list_publish_records()}


@router.delete("/api/publish/records/{record_id}")
async def delete_record(record_id: str) -> dict:
    remove_record(record_id)
    _save()
    return {"ok": True, "records": list_publish_records()}


@router.delete("/api/publish/records")
async def delete_all_records() -> dict:
    removed = clear_records()
    _save()
    return {"ok": True, "removed": removed, "records": []}


@router.post("/api/publish/deletable")
async def post_deletable(request: Request) -> dict:
    body = await json_body(request)
    return analyze_deletable(include_filled=bool(body.get("includeFilled")))


@router.post("/api/publish/mark-deleted")
async def post_mark_deleted(request: Request) -> dict:
    body = await json_body(request)
    marked = mark_deleted(body.get("ids"))
    _save()
    return {"ok": True, "marked": marked, "records": list_publish_records()}


__all__ = ["router"]
