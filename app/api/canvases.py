"""画布 —— 参数读取、素材挂载、增删改查。对应 `server/index.js:686-952`。

画布是「一次生图/生视频的配置」：绑一个 RunningHub 应用或工作流，读回它的可填参数，
用户填值、挂素材，然后 `POST /api/canvases/:id/run` 派给账号池执行。
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Request

from ..canvas_params import field_label, parse_workflow_json, prune_values
from ..config import UPLOADS_DIR
from ..ids import next_id
from ..models import new_canvas
from ..providers import runninghub as rh
from ..scheduler import apply_watermark_off, field_key, rh_field_is_file
from ..shared_files import unlink_if_unused
from ..store import store
from .deps import (
    as_int,
    paths,
    bad_request,
    body_str,
    json_body,
    not_found,
    ok,
    original_name_from,
    raw_body,
    str_or,
    trim,
)

log = logging.getLogger(__name__)

router = APIRouter()

# 上传素材的扩展名 → MIME（原版 `MIME_BY_EXT`）
MIME_BY_EXT = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
    "gif": "image/gif",
    "bmp": "image/bmp",
    "mp4": "video/mp4",
    "mov": "video/quicktime",
    "webm": "video/webm",
    "mkv": "video/x-matroska",
    "avi": "video/x-msvideo",
    "ts": "video/mp2t",
    "m4s": "video/mp4",
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "m4a": "audio/mp4",
    "ogg": "audio/ogg",
}

# 正在跑的状态：删画布时要把它们标成取消
ACTIVE_CANVAS_TASK_STATES = ("queued", "preparing", "remote_queued", "running", "polishing", "web_running")


def find_canvas(canvas_id: str) -> dict | None:
    for canvas in store.canvases:
        if canvas.get("id") == canvas_id:
            return canvas
    return None


def _require_canvas(canvas_id: str) -> dict:
    canvas = find_canvas(canvas_id)
    if not canvas:
        not_found("画布不存在")
    return canvas


# ------------------------------------------------------------------ CRUD


@router.post("/api/canvases")
async def create_canvas(request: Request) -> dict:
    body = await json_body(request)
    name = str_or(body.get("name")).strip() or f"画布 {len(store.canvases) + 1}"
    canvas = new_canvas(name)
    store.canvases.append(canvas)
    store.mark_dirty("canvases")
    store.save()
    store.flush()
    return canvas


@router.patch("/api/canvases/{canvas_id}")
async def patch_canvas(canvas_id: str, request: Request) -> dict:
    canvas = _require_canvas(canvas_id)
    body = await json_body(request)

    if body.get("name") is not None and trim(body.get("name")):
        canvas["name"] = trim(body.get("name"))

    # 应用 / 工作流两种模式的参数结构完全不同，切模式必须把旧参数清空
    mode = body.get("mode")
    if mode in ("app", "workflow") and mode != canvas.get("mode"):
        canvas["mode"] = mode
        canvas["appId"] = ""
        canvas["appName"] = ""
        canvas["fields"] = []
        canvas["values"] = {}
        canvas["assets"] = {}

    if body.get("baseUrl") is not None:
        canvas["baseUrl"] = trim(body.get("baseUrl")) or rh.DEFAULT_BASE
    if "accountId" in body:
        canvas["accountId"] = body.get("accountId") or None
    if "autoRetry" in body:
        canvas["autoRetry"] = bool(body.get("autoRetry"))
    if "maxAttempts" in body:
        canvas["maxAttempts"] = max(1, as_int(body.get("maxAttempts"), 1) or 1)
    if "removeWatermark" in body:
        canvas["removeWatermark"] = bool(body.get("removeWatermark"))
    if "polishAppId" in body:
        canvas["polishAppId"] = trim(body.get("polishAppId"))
    if "webFallback" in body:
        canvas["webFallback"] = bool(body.get("webFallback"))
    if "webUrl" in body:
        canvas["webUrl"] = trim(body.get("webUrl"))
    values = body.get("values")
    if isinstance(values, dict):
        canvas["values"] = {**(canvas.get("values") or {}), **values}
    if "extraParams" in body:
        canvas["extraParams"] = str_or(body.get("extraParams"))

    store.mark_dirty("canvases")
    store.save()
    store.flush()
    return canvas


@router.delete("/api/canvases/{canvas_id}")
async def delete_canvas(canvas_id: str) -> dict:
    canvas = _require_canvas(canvas_id)
    # 先让还在跑的任务收手：直接删画布会让它们跑完找不到落点
    for task in store.tasks:
        if task.get("canvasId") == canvas_id and task.get("status") in ACTIVE_CANVAS_TASK_STATES:
            task["cancelRequested"] = True
    paths = [str((asset or {}).get("localPath") or "") for asset in (canvas.get("assets") or {}).values()]
    store.canvases = [c for c in store.canvases if c.get("id") != canvas_id]
    store.mark_dirty("canvases")
    store.mark_dirty("tasks")
    store.save()
    store.flush()
    for path in paths:
        if path:
            unlink_if_unused(path)
    return ok()


# ------------------------------------------------------------------ 读取参数


def _apply_parsed_fields(canvas: dict) -> list[str]:
    """字段确定之后：裁掉失效的 values/assets，再按需自动关水印"""
    prune_values(canvas)
    adjusted = apply_watermark_off(canvas)
    return [field_label(field=field, value=value) for field, value in adjusted]


@router.post("/api/canvases/{canvas_id}/inspect")
async def inspect_canvas(canvas_id: str, request: Request) -> dict:
    canvas = _require_canvas(canvas_id)
    body = await json_body(request)
    app_id = trim(body.get("appId")) if "appId" in body else str_or(canvas.get("appId"))
    if not app_id:
        bad_request("请填写 webappId / workflowId")

    if canvas.get("mode") == "workflow":
        raw = str_or(body.get("workflowJson")).strip()
        if not raw:
            bad_request("工作流模式需要粘贴导出的 api.json 内容")
        try:
            fields = parse_workflow_json(raw)
        except ValueError as err:
            bad_request(str(err))
        if not fields:
            bad_request("未能从该 JSON 中解析出可填参数")
        canvas["appId"] = app_id
        canvas["appName"] = f"工作流 {app_id}"
        canvas["fields"] = fields
        adjusted = _apply_parsed_fields(canvas)
        store.mark_dirty("canvases")
        store.save()
        store.flush()
        return {**canvas, "watermarkAdjusted": adjusted}

    # 应用模式：拿账号的 key 去问平台「这个应用有哪些参数」
    account = None
    if canvas.get("accountId"):
        account = next((a for a in store.accounts if a.get("id") == canvas.get("accountId")), None)
    if not account:
        account = next((a for a in store.accounts if a.get("enabled") is not False and a.get("apiKey")), None)
    if not account:
        bad_request("请先在「账号管理」中添加至少一个 API Key")

    try:
        info = await rh.inspect_app(
            base_url=canvas.get("baseUrl") or rh.DEFAULT_BASE,
            api_key=account.get("apiKey") or "",
            app_id=app_id,
        )
    except Exception as err:  # noqa: BLE001 - 平台的各种报错都翻成一句人话
        bad_request(str(err))

    fields = info.get("fields") or []
    if not fields:
        bad_request(f"应用「{app_id}」没有返回可填参数")
    canvas["appId"] = app_id
    canvas["appName"] = trim(info.get("name")) or f"应用 {app_id}"
    canvas["fields"] = fields
    if info.get("instanceType") is not None:
        canvas["instanceType"] = info["instanceType"]

    # 新字段若平台给了默认值，就顺手填上，省得用户每个框都点一遍
    values = canvas.get("values") if isinstance(canvas.get("values"), dict) else {}
    for field in fields:
        key = field_key(field)
        if values.get(key) is None and field.get("defaultValue") is not None:
            values[key] = field.get("defaultValue")
    canvas["values"] = values

    adjusted = _apply_parsed_fields(canvas)
    store.mark_dirty("canvases")
    store.save()
    store.flush()
    return {**canvas, "watermarkAdjusted": adjusted}


# ------------------------------------------------------------------ 素材


@router.post("/api/canvases/{canvas_id}/assets/{key}")
async def upload_asset(canvas_id: str, key: str, request: Request) -> dict:
    canvas = _require_canvas(canvas_id)
    field = next((f for f in (canvas.get("fields") or []) if field_key(f) == key), None)
    if not field:
        bad_request("该参数不在当前画布的可填列表中")
    if not rh_field_is_file(field):
        bad_request("该参数不是文件类型")

    buffer = await raw_body(request, limit=800 * 1024 * 1024)
    if not buffer:
        bad_request("没有收到文件内容")
    original_name = original_name_from(request, "upload.bin")
    ext = Path(original_name).suffix.lower()
    if not ext or len(ext) > 8:
        ext = ".bin"
    stored_name = f"{next_id('up')}{ext}"
    media = paths()
    target = Path(media.uploads) / stored_name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(buffer)

    prev = (canvas.get("assets") or {}).get(key) or {}
    canvas.setdefault("assets", {})[key] = {
        "originalName": original_name,
        "localPath": str(target),
        "localUrl": f"/files/uploads/{stored_name}",
        "mimeType": MIME_BY_EXT.get(ext.lstrip("."), "application/octet-stream"),
        "size": len(buffer),
        "uploadedAt": store.now(),
    }
    store.mark_dirty("canvases")
    store.save()
    store.flush()
    prev_path = str(prev.get("localPath") or "")
    if prev_path and prev_path != str(target):
        unlink_if_unused(prev_path)
    return canvas


@router.delete("/api/canvases/{canvas_id}/assets/{key}")
async def delete_asset(canvas_id: str, key: str) -> dict:
    canvas = _require_canvas(canvas_id)
    assets = canvas.get("assets") or {}
    asset = assets.pop(key, None)
    canvas["assets"] = assets
    store.mark_dirty("canvases")
    store.save()
    store.flush()
    if asset and asset.get("localPath"):
        unlink_if_unused(str(asset["localPath"]))
    return canvas


# ------------------------------------------------------------------ 额外参数


@router.post("/api/canvases/{canvas_id}/extra-params")
async def set_extra_params(canvas_id: str, request: Request) -> dict:
    canvas = _require_canvas(canvas_id)
    body = await json_body(request)
    raw = str_or(body.get("extraParams"))
    if raw.strip():
        import json

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as err:
            bad_request(f"额外参数格式错误：{err}")
        if not isinstance(parsed, list):
            bad_request('额外参数必须是 JSON 数组，例如 [{"name":"seed","value":12345}]')
    canvas["extraParams"] = raw
    store.mark_dirty("canvases")
    store.save()
    store.flush()
    return canvas


__all__ = ["router", "find_canvas", "MIME_BY_EXT"]
