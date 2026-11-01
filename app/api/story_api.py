"""剧本 —— 分镜模型配置、剧本增删查、定妆照、批量出图。对应 `server/index.js:1960-2048`。

一共 9 条：
  · `GET  /api/story-model`      分镜模型配置（回显 apiKey 时是掩码）
  · `POST /api/story-model`      保存分镜模型配置
  · `GET  /api/storyboards`      全部剧本快照
  · `POST /api/storyboards`      一句话/题材 → 一份剧本（只出文字，不出图）
  · `GET  /api/storyboards/:id`  单份剧本（读之前先 sync，把出图状态刷成最新的）
  · `DELETE /api/storyboards/:id`
  · `POST /api/storyboards/:id/cast`  给某个角色出定妆照
  · `POST /api/storyboards/:id/draw`  给所有还没出图的镜头批量建任务

「先 sync 再回快照」这一点很关键：剧本里的图片状态是**现查 image_tasks** 得来的，
不存快照；任务被重试、被裁剪之后，不 sync 就会和真实结果对不上。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request

from ..storyboard import (
    all_storyboard_snapshots,
    cast_character,
    draw_shots,
    find_storyboard,
    remove_storyboard,
    save_story_model,
    story_model_snapshot,
    storyboard_snapshot,
    sync_storyboards,
    write_script,
)
from .deps import bad_request, json_body, not_found

log = logging.getLogger(__name__)

router = APIRouter()


# ------------------------------------------------------------------ 分镜模型


@router.get("/api/story-model")
async def get_story_model() -> dict:
    return story_model_snapshot()


@router.post("/api/story-model")
async def post_story_model(request: Request) -> dict:
    body = await json_body(request)
    try:
        return save_story_model(body)
    except Exception as err:  # noqa: BLE001 - 原版 catch 后回 400
        raise bad_request(str(err)) from err


# ------------------------------------------------------------------ 剧本 CRUD


@router.get("/api/storyboards")
async def list_storyboards() -> list:
    return all_storyboard_snapshots()


@router.post("/api/storyboards")
async def post_storyboard(request: Request) -> dict:
    body = await json_body(request)
    try:
        board = await write_script(
            idea=body.get("idea"),
            shot_count=body.get("shotCount"),
            style=body.get("style"),
            # 人物参考图的图库 id 数组：给了就照着图里的人写角色，并把图直接当定妆照
            photo_ids=body.get("photoIds"),
        )
        return storyboard_snapshot(board)
    except Exception as err:  # noqa: BLE001
        raise bad_request(str(err)) from err


@router.get("/api/storyboards/{board_id}")
async def get_storyboard(board_id: str) -> dict:
    board = find_storyboard(board_id)
    if not board:
        raise not_found("这份剧本不存在")
    sync_storyboards()
    return storyboard_snapshot(board)


@router.delete("/api/storyboards/{board_id}")
async def delete_storyboard(board_id: str) -> dict:
    board = find_storyboard(board_id)
    if not board:
        raise not_found("这份剧本不存在")
    remove_storyboard(board)
    return {"ok": True}


# ------------------------------------------------------------------ 出图


@router.post("/api/storyboards/{board_id}/cast")
async def post_cast(board_id: str, request: Request) -> dict:
    board = find_storyboard(board_id)
    if not board:
        raise not_found("这份剧本不存在")
    body = await json_body(request)
    try:
        task = cast_character(
            board,
            str(body.get("name") or ""),
            model_id=body.get("modelId"),
            size=body.get("size"),
        )
        return {"task": task, "board": storyboard_snapshot(board)}
    except Exception as err:  # noqa: BLE001
        raise bad_request(str(err)) from err


@router.post("/api/storyboards/{board_id}/draw")
async def post_draw(board_id: str, request: Request) -> dict:
    board = find_storyboard(board_id)
    if not board:
        raise not_found("这份剧本不存在")
    body = await json_body(request)
    try:
        created = draw_shots(board, model_id=body.get("modelId"), size=body.get("size"))
        return {"created": len(created), "board": storyboard_snapshot(board)}
    except Exception as err:  # noqa: BLE001
        raise bad_request(str(err)) from err


__all__ = ["router"]
