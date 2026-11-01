"""任务 —— 画布任务队列的提交、查询、重试、取消、删除、取回。对应 `server/index.js:2089-2357`。

九条：
  · `POST   /api/canvases/:id/run`          把一张画布提交成一条任务
  · `POST   /api/canvases/run-all`         所有画布各提交一条
  · `GET    /api/tasks`                    任务列表（可按画布 / 账号过滤）
  · `POST   /api/tasks/:id/cancel`         取消
  · `POST   /api/tasks/:id/refresh-result` 按远端任务号重查并下载（只查不重发）
  · `POST   /api/tasks/import`             按平台任务号收编一条本地没有的任务
  · `POST   /api/tasks/fetch-missing`      一键取回所有结果文件丢失的任务
  · `POST   /api/tasks/:id/retry`          重试
  · `DELETE /api/tasks/:id`                删除记录（只删记录，不动成品文件）

路径里带 `:id` 的必须排在 `/api/tasks/import`、`/api/tasks/fetch-missing` 后面声明 ——
FastAPI 按声明顺序匹配，写反了 `import` 会被当成 task_id 吃掉。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request

from ..api.canvases import find_canvas
from ..scheduler import (
    cancel_task_request,
    create_task,
    fetch_missing_results,
    find_task,
    get_paths,
    import_remote_task,
    refresh_task_result,
    retry_task,
)
from ..store import store
from .deps import bad_request, json_body, not_found

log = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------- 提交


@router.post("/api/canvases/run-all")
async def post_run_all() -> dict:
    """所有画布各提交一条任务。单条失败不影响其它，错误按「画布名：原因」收集回来。"""
    created: list[dict] = []
    errors: list[str] = []
    for canvas in store.canvases:
        try:
            created.append(
                create_task(canvas, pinned_account_id=canvas.get("accountId") or None)
            )
        except Exception as err:  # noqa: BLE001 - 原版逐条 catch
            errors.append(f"{canvas.get('name') or canvas.get('id')}：{err}")
    store.flush()
    return {"created": len(created), "errors": errors}


@router.post("/api/canvases/{canvas_id}/run")
async def post_run(canvas_id: str, request: Request) -> dict:
    canvas = find_canvas(canvas_id)
    if not canvas:
        raise not_found("画布不存在")
    body = await json_body(request)
    try:
        task = create_task(canvas, pinned_account_id=body.get("accountId") or None)
        store.flush()
        return task
    except Exception as err:  # noqa: BLE001
        raise bad_request(str(err)) from err


# ---------------------------------------------------------------- 查询


@router.get("/api/tasks")
async def get_tasks(
    canvasId: str | None = None,
    accountId: str | None = None,
    limit: int | None = None,
) -> list:
    """任务列表。「API 任务」弹窗要翻更久的历史，所以上限放到 400（库本身最多留 400 条）。"""
    size = min(400, max(1, int(limit or 0) or 120))
    tasks = store.tasks
    if canvasId:
        tasks = [t for t in tasks if t.get("canvasId") == canvasId]
    if accountId:
        tasks = [t for t in tasks if t.get("accountId") == accountId]
    return tasks[:size]


# ---------------------------------------------------------------- 取回（静态路径，必须排在 :id 之前）


@router.post("/api/tasks/import")
async def post_import(request: Request) -> dict:
    body = await json_body(request)
    try:
        return await import_remote_task(
            remote_task_id=body.get("remoteTaskId"),
            account_id=body.get("accountId"),
            canvas_id=body.get("canvasId"),
        )
    except Exception as err:  # noqa: BLE001
        raise bad_request(str(err)) from err


@router.post("/api/tasks/fetch-missing")
async def post_fetch_missing() -> dict:
    try:
        return await fetch_missing_results()
    except Exception as err:  # noqa: BLE001
        raise bad_request(str(err)) from err


# ---------------------------------------------------------------- 单条操作


@router.post("/api/tasks/{task_id}/cancel")
async def post_cancel(task_id: str) -> dict:
    task = find_task(task_id)
    if not task:
        raise not_found("任务不存在")
    return cancel_task_request(task)


@router.post("/api/tasks/{task_id}/refresh-result")
async def post_refresh_result(task_id: str) -> dict:
    task = find_task(task_id)
    if not task:
        raise not_found("任务不存在")
    try:
        return await refresh_task_result(task)
    except Exception as err:  # noqa: BLE001
        raise bad_request(str(err)) from err


@router.post("/api/tasks/{task_id}/retry")
async def post_retry(task_id: str) -> dict:
    task = find_task(task_id)
    if not task:
        raise not_found("任务不存在")
    try:
        return retry_task(task)
    except Exception as err:  # noqa: BLE001
        raise bad_request(str(err)) from err


@router.delete("/api/tasks/{task_id}")
async def delete_task(task_id: str) -> dict:
    """删记录。只删这条记录，已经下载到本地的成品文件留在原处不删。"""
    rows = store.tasks
    index = next((i for i, t in enumerate(rows) if t.get("id") == task_id), -1)
    if index < 0:
        raise not_found("任务不存在")
    del rows[index]
    store.mark_dirty("tasks")
    store.save()
    store.flush()
    return {"ok": True}


__all__ = ["router"]
