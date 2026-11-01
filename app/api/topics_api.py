"""话题库 —— 对应 `server/index.js:1662-1694`。

抖音发布时话题决定曝光。这里的库是「采集到的话题先攒着，发布时从库里挑」，
名字按 `#去空白截30` 归一化去重。
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from ..topics import (
    add_topics,
    clear_topics,
    find_topic,
    mark_topics_used,
    remove_topic,
    topic_list,
    topic_stats,
)
from ..store import store
from .deps import bad_request, json_body, not_found, ok, str_or

router = APIRouter()


def _bundle(**extra) -> dict:
    return {"topics": topic_list(), "stats": topic_stats(), **extra}


@router.get("/api/topics")
async def get_topics(q: str = "") -> dict:
    return {"topics": topic_list(q=q), "stats": topic_stats()}


@router.post("/api/topics")
async def post_topics(request: Request) -> dict:
    body = await json_body(request)
    names = body.get("names") if body.get("names") is not None else body.get("name")
    result = add_topics(
        names,
        source=str_or(body.get("source")) or "douyin",
        source_desc=str_or(body.get("sourceDesc")),
    )
    store.save()
    store.flush()
    return _bundle(**result)


@router.post("/api/topics/used")
async def topics_used(request: Request) -> dict:
    body = await json_body(request)
    touched = mark_topics_used(body.get("names"))
    store.save()
    store.flush()
    return _bundle(touched=touched)


@router.delete("/api/topics/{topic_id}")
async def delete_topic(topic_id: str) -> dict:
    topic = find_topic(topic_id)
    if not topic:
        not_found("这个话题不在库里")
    remove_topic(topic)
    store.save()
    store.flush()
    return _bundle(ok=True)


@router.delete("/api/topics")
async def delete_all_topics() -> dict:
    removed = clear_topics()
    store.save()
    store.flush()
    return {"ok": True, "removed": removed}


__all__ = ["router"]
