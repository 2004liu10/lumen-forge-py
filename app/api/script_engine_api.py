"""剧本引擎 —— 本地 GPT 写剧本的探测接口。对应 `server/index.js:1977`。

只有一条：`GET /api/script-engine`。前端在「剧本模型」面板上点「检测」时调它，
返回引擎是否可用、可执行文件路径、版本号，以及不可用的原因。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter

from ..script_writer import script_engine_info
from .deps import bad_request

log = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/script-engine")
async def get_script_engine() -> dict:
    try:
        return await script_engine_info()
    except Exception as err:  # noqa: BLE001 - 原版 catch 后回 400
        raise bad_request(str(err)) from err


__all__ = ["router"]
