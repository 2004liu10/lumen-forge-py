"""路由层公共件 —— 复刻 `server/index.js` 里的那套 Express 习惯。

原版全靠 `req.body` / `res.status(400).json({error})` / `express.raw()`。FastAPI 里
这些都没有天然对应物，所以这里把「读 JSON 体」「读裸体」「抛 400」「取原始文件名」
统一收口，各路由文件只写业务。
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import unquote

from fastapi import Request
from fastapi.responses import JSONResponse, Response

from ..config import MAX_REF_BYTES


class HttpError(Exception):
    """带状态码的业务错误，由 main.py 的异常处理器翻译成 `{error: 消息}`"""

    def __init__(self, status: int, message: str, extra: dict | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra or {}


def fail(status: int, message: str, **extra: Any) -> None:
    raise HttpError(status, message, extra)


def not_found(message: str) -> None:
    raise HttpError(404, message)


def bad_request(message: str) -> None:
    raise HttpError(400, message)


def error_response(status: int, message: str, extra: dict | None = None) -> JSONResponse:
    body: dict[str, Any] = {"error": message}
    if extra:
        body.update(extra)
    return JSONResponse(body, status_code=status)


async def json_body(request: Request) -> dict:
    """读 JSON 体。空体当 `{}`，与原版 `req.body || {}` 一致。"""
    raw = await request.body()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        bad_request("请求体不是合法的 JSON")
    if isinstance(data, list):
        # 少数接口（额外参数）直接收数组，原版就是这么用的
        return {"__array__": data}
    if not isinstance(data, dict):
        bad_request("请求体必须是 JSON 对象")
    return data


async def raw_body(request: Request, *, limit: int | None = None) -> bytes:
    """读裸体（图片 / 视频上传都走这个，原版用 express.raw 而非 multipart）"""
    raw = await request.body()
    if limit is not None and len(raw) > limit:
        fail(413, f"上传内容太大（{len(raw) / 1048576:.1f}MB），上限 {limit / 1048576:.0f}MB")
    return raw


async def raw_body_limited(request: Request) -> bytes:
    """参考图上传的统一上限（原版 `express.raw({limit:'40mb'})` + MAX_REF_BYTES 双闸）"""
    return await raw_body(request, limit=40 * 1024 * 1024)


def original_name_from(request: Request, fallback: str) -> str:
    """从请求头还原原始文件名（原版 `originalNameFrom(req, fallback)`）。

    原版 `server/index.js:239` 读的是 **`x-filename`**（全项目的上传都发这个头），
    `Content-Disposition` 只是兜底。注意不是 `x-file-name` —— 少个 `name` 就读不到。
    """
    head = request.headers.get("x-filename")
    if head:
        try:
            return unquote(head)
        except Exception:  # noqa: BLE001 - 解不出来就用原文
            return head
    disp = request.headers.get("content-disposition") or ""
    marker = "filename*=" if "filename*=" in disp else ("filename=" if "filename=" in disp else "")
    if marker:
        piece = disp.split(marker, 1)[1].split(";")[0].strip().strip('"')
        if piece.lower().startswith("utf-8''"):
            piece = piece[7:]
        try:
            return unquote(piece)
        except Exception:  # noqa: BLE001
            return piece
    return fallback


def no_content() -> Response:
    return Response(status_code=204)


def ok(**fields: Any) -> dict:
    return {"ok": True, **fields}


def as_int(value: Any, fallback: int = 0) -> int:
    """`Number(v) || fallback` 的 Python 版：NaN / 非数 / 0 都退回落值"""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return fallback
    if num != num or num in (float("inf"), float("-inf")):
        return fallback
    return int(num) if num else fallback


def clamp_int(value: Any, *, low: int, high: int, fallback: int) -> int:
    num = as_int(value, fallback)
    return max(low, min(high, num))


def str_or(value: Any, fallback: str = "") -> str:
    if value is None:
        return fallback
    return str(value)


def trim(value: Any) -> str:
    return str(value or "").strip()


def body_str(body: dict, key: str, fallback: str = "") -> str:
    return trim(body.get(key)) if key in body else fallback


_PATHS_CACHE: list[Any] = []


def paths() -> Any:
    """当前生效的媒体目录集合。

    正常路径是 `main.py` 启动时把同一个 `Paths` 实例塞给各业务模块；单测或脚本里
    没走启动流程时，这里补一个（缓存住，别每次新建）。
    """
    from ..scheduler import get_paths

    try:
        return get_paths()
    except RuntimeError:
        if not _PATHS_CACHE:
            from ..paths import Paths
            from ..store import store

            _PATHS_CACHE.append(Paths(store))
        return _PATHS_CACHE[0]


def set_paths(paths_obj: Any) -> None:
    """由 `main.py` 注入，让路由层与业务模块共用同一个实例"""
    _PATHS_CACHE.clear()
    _PATHS_CACHE.append(paths_obj)


__all__ = [
    "HttpError",
    "fail",
    "not_found",
    "bad_request",
    "error_response",
    "json_body",
    "raw_body",
    "raw_body_limited",
    "original_name_from",
    "no_content",
    "ok",
    "as_int",
    "clamp_int",
    "str_or",
    "trim",
    "body_str",
    "paths",
    "set_paths",
    "MAX_REF_BYTES",
]
