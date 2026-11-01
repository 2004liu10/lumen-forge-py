"""画布 AI —— 编导拆分镜 + 右侧聊天。对应 `server/index.js:2049-2090`。

三条路由：
  · `GET  /api/canvas/director`  当前引擎状态（前端进页面时探测一次）
  · `POST /api/canvas/director`  一段文字 → 一份分镜（无上下文，纯函数）
  · `POST /api/canvas/chat`      聊天：判意图、回一句话，必要时内部再走上面那条

原版这三条都 try/catch 后回 400 `{error}`，所以业务异常一律翻成 400 ——
前端拿到的是人话（"先贴一段剧本，或者写一句题材"），不是 500 堆栈。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request

from ..canvas_chat import chat_with_canvas
from ..canvas_director import direct_storyboard, director_info
from .deps import bad_request, json_body

log = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/canvas/director")
async def get_director() -> dict:
    return director_info()


@router.post("/api/canvas/director")
async def post_director(request: Request) -> dict:
    body = await json_body(request)
    try:
        return await direct_storyboard(
            script=body.get("script"),
            shot_count=body.get("shotCount"),
            style=body.get("style"),
            want_video=body.get("wantVideo") is not False,
        )
    except Exception as err:  # noqa: BLE001 - 原版 catch 后回 400
        raise bad_request(str(err)) from err


@router.post("/api/canvas/chat")
async def post_chat(request: Request) -> dict:
    body = await json_body(request)
    try:
        return await chat_with_canvas(
            messages=body.get("messages"),
            scene=body.get("scene"),
            prefs=body.get("prefs"),
            # 人物参考图：前端先上传到图库，这里只收 id
            photo_ids=body.get("photoIds"),
        )
    except Exception as err:  # noqa: BLE001
        raise bad_request(str(err)) from err


__all__ = ["router"]
