"""Topaz 提升画质 —— 引擎配置、候选清单、单条/批量提升、取消、还原、本地文件夹预览。
对应 `server/index.js:2163-2340`，一共 9 条。

  · `GET  /api/topaz/candidates`   批量弹窗的候选清单（待提升 / 已提升）
  · `POST /api/topaz/enhance`      提升某一条
  · `POST /api/topaz/enhance-batch` 批量排队
  · `POST /api/topaz/cancel`       取消
  · `POST /api/topaz/restore`      还原原片
  · `POST /api/topaz/clear-failed` 清空失败记录
  · `POST /api/topaz/config`       手动指定安装目录
  · `POST /api/topaz/look`         质感档
  · `POST /api/topaz/local`        本地文件夹：列视频 / 记住目录
  · `GET  /api/topaz/local-file`   本地视频流（必须支持 Range，否则缩略图全黑、进度条拖不动）

`local-file` 的安全闸是**双重**的：既要「绝对路径 + 是视频」，还要落在用户选过的
文件夹或本程序自己管的媒体目录里。只校验前者等于开放按路径探测本机任意视频文件。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import Response, StreamingResponse

from ..config import DATA_DIR
from ..scheduler import get_paths
from ..topaz import (
    cancel_enhance,
    clear_topaz_failed_jobs,
    get_local_dir,
    is_local_video_file,
    norm_source,
    restore,
    scan_local_dir,
    set_local_dir,
    set_topaz_dir,
    set_topaz_look,
    start_batch,
    start_enhance,
    topaz_candidates,
)
from .deps import bad_request, json_body

log = logging.getLogger(__name__)

router = APIRouter()

#: 本地视频扩展名 → MIME，预览流要用
VIDEO_MIME = {
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".mov": "video/quicktime",
    ".mkv": "video/x-matroska",
    ".webm": "video/webm",
    ".avi": "video/x-msvideo",
    ".flv": "video/x-flv",
    ".wmv": "video/x-ms-wmv",
    ".mpeg": "video/mpeg",
    ".mpg": "video/mpeg",
}


# ------------------------------------------------------------------ 候选 / 提升


@router.get("/api/topaz/candidates")
async def get_candidates() -> dict:
    try:
        return topaz_candidates()
    except Exception as err:  # noqa: BLE001 - 原版 catch 后回 400
        raise bad_request(str(err)) from err


@router.post("/api/topaz/enhance")
async def post_enhance(request: Request) -> dict:
    body = await json_body(request)
    try:
        return start_enhance(
            source=norm_source(body.get("source")),
            task_id=body.get("taskId"),
            index=int(body.get("index") or 0),
            local_path=body.get("localPath"),
            tier=body.get("tier"),
        )
    except Exception as err:  # noqa: BLE001
        raise bad_request(str(err)) from err


@router.post("/api/topaz/enhance-batch")
async def post_enhance_batch(request: Request) -> dict:
    body = await json_body(request)
    try:
        return start_batch(items=body.get("items"), tier=body.get("tier"))
    except Exception as err:  # noqa: BLE001
        raise bad_request(str(err)) from err


@router.post("/api/topaz/cancel")
async def post_cancel(request: Request) -> dict:
    body = await json_body(request)
    try:
        return cancel_enhance(
            job_id=body.get("jobId"),
            source=norm_source(body.get("source")),
            task_id=body.get("taskId"),
            # 原版这里是 `index === undefined ? 0 : Number(index)`：没传当 0，传 null 当 NaN→0
            index=int(body.get("index") or 0),
        )
    except Exception as err:  # noqa: BLE001
        raise bad_request(str(err)) from err


@router.post("/api/topaz/restore")
async def post_restore(request: Request) -> dict:
    body = await json_body(request)
    try:
        return restore(
            source=norm_source(body.get("source")),
            task_id=body.get("taskId"),
            index=int(body.get("index") or 0),
        )
    except Exception as err:  # noqa: BLE001
        raise bad_request(str(err)) from err


@router.post("/api/topaz/clear-failed")
async def post_clear_failed() -> dict:
    try:
        return clear_topaz_failed_jobs()
    except Exception as err:  # noqa: BLE001
        raise bad_request(str(err)) from err


# ------------------------------------------------------------------ 配置


@router.post("/api/topaz/config")
async def post_config(request: Request) -> dict:
    body = await json_body(request)
    try:
        return set_topaz_dir(body.get("dir"))
    except Exception as err:  # noqa: BLE001
        raise bad_request(str(err)) from err


@router.post("/api/topaz/look")
async def post_look(request: Request) -> dict:
    body = await json_body(request)
    try:
        return {"ok": True, **set_topaz_look(str(body.get("look") or ""))}
    except Exception as err:  # noqa: BLE001
        raise bad_request(str(err)) from err


# ------------------------------------------------------------------ 本地文件夹


@router.post("/api/topaz/local")
async def post_local(request: Request) -> dict:
    """带 dir → 记住它并扫一遍（传空串 = 清掉）；不带 dir → 按记住的目录重扫。"""
    try:
        body = await json_body(request)
        if "dir" not in body:
            directory = get_local_dir()
            return {**scan_local_dir(directory), "dir": directory}
        return set_local_dir(str(body.get("dir") or "").strip())
    except Exception as err:  # noqa: BLE001
        raise bad_request(str(err)) from err


def _allowed_roots() -> list[Path]:
    """只服务「用户自己在提升画质里选过的文件夹」和本程序自己管理的媒体目录"""
    paths = get_paths()
    raw = [get_local_dir(), paths.outputs, paths.topaz, paths.videos, DATA_DIR]
    roots: list[Path] = []
    for item in raw:
        if not item:
            continue
        try:
            roots.append(Path(str(item)).resolve())
        except OSError:
            continue
    return roots


def _is_inside(path: Path, roots: list[Path]) -> bool:
    for root in roots:
        try:
            rel = path.relative_to(root)
        except ValueError:
            continue
        # rel 为空 = 正好是 root 本身，也算在内
        return True
    return False


@router.get("/api/topaz/local-file")
async def get_local_file(request: Request, path: str = "") -> Response:
    """把本地视频流给前端当缩略图。

    必须是绝对路径 + 存在 + 扩展名是视频 + 落在允许的目录里，否则一律 404。
    支持 `Range: bytes=a-b`（返回 206 + Content-Range），否则浏览器拿不到首帧。
    """
    file = str(path or "")
    if not is_local_video_file(file):
        return Response(status_code=404)
    try:
        abs_path = Path(file).resolve()
    except OSError:
        return Response(status_code=404)
    if not _is_inside(abs_path, _allowed_roots()):
        return Response(status_code=404)
    try:
        size = abs_path.stat().st_size
    except OSError:
        return Response(status_code=404)

    mime = VIDEO_MIME.get(abs_path.suffix.lower()) or "application/octet-stream"

    # 解析 Range。注意原版的正则只认 `bytes=` 开头，其它格式一律当成整段返回
    range_header = request.headers.get("range") or ""
    start, end = 0, size - 1
    partial = False
    if range_header.startswith("bytes="):
        import re

        match = re.match(r"bytes=(\d*)-(\d*)", range_header)
        if match:
            partial = True
            if match.group(1):
                start = int(match.group(1))
            if match.group(2):
                end = int(match.group(2))
    if start < 0:
        start = 0
    if end >= size:
        end = size - 1
    if size == 0:
        return Response(status_code=200, headers={"Accept-Ranges": "bytes", "Content-Length": "0"})
    if start > end:
        return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})

    headers = {
        "Content-Type": mime,
        "Accept-Ranges": "bytes",
        "Content-Length": str(end - start + 1),
    }
    status = 200
    if partial:
        status = 206
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"

    def stream():
        with open(abs_path, "rb") as handle:
            handle.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk = handle.read(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    return StreamingResponse(stream(), status_code=status, headers=headers, media_type=mime)


__all__ = ["router"]
