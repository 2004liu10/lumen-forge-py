"""存储位置 —— `POST /api/storage`。对应 `server/index.js:389-398`。

只有一条，但它是「设置 → 存储位置」的全部：改某个媒体目录、或恢复默认。
`dir` 传空字符串等于恢复默认（原版把这条规则放在 `setStorageDir` 里）。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request

from ..scheduler import get_paths
from .deps import bad_request, json_body

log = logging.getLogger(__name__)

router = APIRouter()


@router.post("/api/storage")
async def post_storage(request: Request) -> dict:
    body = await json_body(request)
    paths = get_paths()
    key = str(body.get("key") or "")
    try:
        if body.get("reset"):
            result = paths.reset_dir(key)
        else:
            result = paths.set_dir(key, body.get("dir"))
        return {"ok": True, **result, "items": paths.snapshot()}
    except Exception as err:  # noqa: BLE001 - 原版 catch 后回 400
        raise bad_request(str(err)) from err


__all__ = ["router"]
