"""图片模型与图片任务 —— 对应 `server/index.js:957-1350`。

图片模型是「一个厂商的某一档模型 + 一份 Key」；图片任务是「用某个模型生成一批图」。
图片任务与 RunningHub 账号池完全独立，走各厂商自己的 Key，队列全局串行（
见 `app/image_jobs.py`）。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import APIRouter, Request

from ..canvas_params import field_label
from ..config import MAX_REF_BYTES
from ..ids import next_id
from ..image_jobs import (
    cancel_image_task,
    create_image_task,
    create_ref_from_image,
    create_ref_from_upload,
    find_image_model,
    find_image_ref,
    find_image_task,
    image_model_snapshot,
    remove_image_ref,
    reuse_image_refs,
    retry_image_task,
)
from ..models import new_image_model, new_image_task
from ..providers import doubao_bridge
from ..providers import imagegen
from ..providers.runninghub import DEFAULT_BASE
from ..scheduler import field_key, rh_field_is_file
from ..shared_files import share_file, unlink_if_unused
from ..store import store
from .canvases import MIME_BY_EXT, find_canvas
from .deps import (
    as_int,
    bad_request,
    fail,
    json_body,
    not_found,
    ok,
    original_name_from,
    paths,
    raw_body,
    raw_body_limited,
    str_or,
    trim,
)

log = logging.getLogger(__name__)

router = APIRouter()

IMAGE_PROTOCOLS = (
    "ark",
    "openai",
    "zhipu",
    "jimeng",
    "wanxiang",
    "hunyuan",
    "minimax",
    "cloudflare",
)

_PROTOCOL_LABEL = (
    "ark（火山方舟）、openai（兼容接口）、zhipu（智谱 CogView）、jimeng（即梦）、"
    "wanxiang（通义万相）、hunyuan（腾讯混元生图）、minimax（MiniMax 海螺）"
    "或 cloudflare（Cloudflare Workers AI）"
)


def _default_base_for(protocol: str) -> str:
    return {
        "ark": imagegen.ARK_BASE,
        "zhipu": imagegen.ZHIPU_BASE,
        "jimeng": imagegen.JIMENG_BASE,
        "wanxiang": imagegen.WANXIANG_BASE,
        "hunyuan": imagegen.HUNYUAN_BASE,
        "minimax": imagegen.MINIMAX_BASE,
        "cloudflare": imagegen.CLOUDFLARE_BASE,
    }.get(protocol, "https://api.openai.com/v1")


def apply_image_model_body(model: dict, body: dict) -> dict:
    """把请求体里的字段套到模型上。抛 `ValueError` 由路由翻成 400。"""
    protocol = body.get("protocol")
    if protocol is not None:
        protocol = trim(protocol)
        if protocol not in IMAGE_PROTOCOLS:
            raise ValueError(f"协议只支持 {_PROTOCOL_LABEL}")
        changed = protocol != model.get("protocol")
        model["protocol"] = protocol
        # 换协议而用户没显式给地址时，用新协议的家地址（否则会拿旧家的地址去请求）
        if changed and not trim(body.get("baseUrl")):
            model["baseUrl"] = _default_base_for(protocol)

    if body.get("name") is not None and trim(body.get("name")):
        model["name"] = trim(body.get("name"))
    if "baseUrl" in body:
        model["baseUrl"] = trim(body.get("baseUrl"))
    # 密钥类字段只在「传了非空值」时覆盖：前端会把掩码（****）回显回来，
    # 直接存下去会把真 Key 冲掉
    for key in ("apiKey", "akSecret", "accountId"):
        if trim(body.get(key)):
            model[key] = trim(body.get(key))
    for key in ("model", "size"):
        if key in body:
            model[key] = trim(body.get(key))
    for key in ("noWatermark", "supportsImageInput", "enabled"):
        if key in body:
            model[key] = bool(body.get(key))
    if "extraParams" in body:
        raw = trim(body.get("extraParams"))
        if raw:
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as err:
                raise ValueError(f"额外参数格式错误：{err}") from err
            if not isinstance(parsed, dict):
                raise ValueError('额外参数必须是 JSON 对象，例如 {"seed": 12345}')
        model["extraParams"] = raw
    return model


@router.post("/api/image-models")
async def create_image_model(request: Request) -> dict:
    body = await json_body(request)
    if not trim(body.get("name")):
        bad_request("请填写模型名称")
    model = new_image_model({"name": trim(body.get("name"))})
    try:
        apply_image_model_body(model, body)
    except ValueError as err:
        bad_request(str(err))
    store.image_models.append(model)
    store.mark_dirty("image_models")
    store.save()
    store.flush()
    return image_model_snapshot(model)


@router.patch("/api/image-models/{model_id}")
async def patch_image_model(model_id: str, request: Request) -> dict:
    model = find_image_model(model_id)
    if not model:
        not_found("图片模型不存在")
    body = await json_body(request)
    try:
        apply_image_model_body(model, body)
    except ValueError as err:
        bad_request(str(err))
    store.mark_dirty("image_models")
    store.save()
    store.flush()
    return image_model_snapshot(model)


@router.delete("/api/image-models/{model_id}")
async def delete_image_model(model_id: str) -> dict:
    if not find_image_model(model_id):
        not_found("图片模型不存在")
    store.image_models = [m for m in store.image_models if m.get("id") != model_id]
    store.mark_dirty("image_models")
    store.save()
    store.flush()
    return ok()


# ------------------------------------------------------------------ 图片任务


@router.post("/api/image-tasks")
async def post_image_task(request: Request) -> dict:
    body = await json_body(request)
    try:
        task = create_image_task(
            model_id=str_or(body.get("modelId")) or None,
            prompt=str_or(body.get("prompt")),
            size=str_or(body.get("size")),
            count=as_int(body.get("count"), 1) or 1,
            ref_ids=body.get("refIds") if isinstance(body.get("refIds"), list) else None,
        )
    except ValueError as err:
        bad_request(str(err))
    return task


@router.post("/api/image-tasks/{task_id}/retry")
async def retry_task(task_id: str) -> dict:
    task = find_image_task(task_id)
    if not task:
        not_found("生成记录不存在")
    try:
        return retry_image_task(task)
    except ValueError as err:
        bad_request(str(err))


@router.post("/api/image-tasks/{task_id}/cancel")
async def cancel_task(task_id: str) -> dict:
    task = find_image_task(task_id)
    if not task:
        not_found("生成记录不存在")
    try:
        return cancel_image_task(task)
    except ValueError as err:
        bad_request(str(err))


@router.delete("/api/image-tasks/{task_id}")
async def delete_image_task(task_id: str) -> dict:
    task = find_image_task(task_id)
    if not task:
        not_found("生成记录不存在")
    paths_to_drop = [str((img or {}).get("localPath") or "") for img in (task.get("images") or [])]
    store.image_tasks = [t for t in store.image_tasks if t.get("id") != task_id]
    store.mark_dirty("image_tasks")
    store.save()
    store.flush()
    for path in paths_to_drop:
        if path:
            unlink_if_unused(path)
    return ok()


@router.post("/api/image-tasks/{task_id}/ref")
async def task_image_to_ref(task_id: str, request: Request) -> dict:
    """把生成结果里的某一张存进参考图库"""
    task = find_image_task(task_id)
    if not task:
        not_found("生成记录不存在")
    body = await json_body(request)
    images = task.get("images") or []
    image = None
    if body.get("imageId"):
        image = next((i for i in images if i.get("id") == body["imageId"]), None)
    elif images:
        image = images[0]
    if not image:
        bad_request("找不到这张图片")
    try:
        ref = create_ref_from_image(task, image)
    except ValueError as err:
        bad_request(str(err))
    return ref


@router.post("/api/image-tasks/{task_id}/reuse")
async def reuse_task_refs(task_id: str) -> dict:
    task = find_image_task(task_id)
    if not task:
        not_found("生成记录不存在")
    return {"refs": reuse_image_refs(task)}


@router.post("/api/image-tasks/{task_id}/to-canvas")
async def task_image_to_canvas(task_id: str, request: Request) -> dict:
    """把一张生成图直接挂到画布的图片参数上（不复制文件，只共享路径）"""
    task = find_image_task(task_id)
    if not task:
        not_found("生成记录不存在")
    body = await json_body(request)
    image = next(
        (i for i in (task.get("images") or []) if body.get("imageId") and i.get("id") == body.get("imageId")),
        None,
    ) or (task.get("images") or [None])[0]
    if not image:
        bad_request("找不到这张图片")
    if not Path(str(image.get("localPath") or "")).is_file():
        bad_request("这张图片的本地文件已被删除，无法送入")

    canvas = find_canvas(str_or(body.get("canvasId"))) if body.get("canvasId") else None
    if not canvas:
        bad_request("请选择要送入的画布")
    fields = canvas.get("fields") or []
    field = None
    if body.get("fieldKey"):
        field = next((f for f in fields if field_key(f) == body["fieldKey"]), None)
    else:
        field = next((f for f in fields if str(f.get("fieldType") or "").upper() == "IMAGE"), None)
    if not field:
        bad_request(f"画布「{canvas.get('name')}」还没有图片输入参数，请先在该画布点「读取参数」")
    if not rh_field_is_file(field):
        bad_request("选中的参数不是图片类型")

    media = paths()
    local_path = str(Path(str(image["localPath"])).resolve())
    ext = Path(local_path).suffix.lower()
    if local_path.startswith(str(media.generated)):
        gen_prefix = "/files/generated/refs/" if local_path.startswith(str(media.refs)) else "/files/generated/"
    else:
        gen_prefix = "/files/uploads/"
    target_key = field_key(field)
    prev = (canvas.get("assets") or {}).get(target_key) or {}
    shared = share_file(
        local_path=local_path,
        original_name=str(image.get("fileName") or Path(local_path).name),
        mime_type=MIME_BY_EXT.get(ext.lstrip("."), "image/png"),
        size=as_int(image.get("size"), 0),
        origin=f"imageTask:{task.get('id')}",
    )
    canvas.setdefault("assets", {})[target_key] = {
        **shared,
        "localUrl": f"{gen_prefix}{Path(local_path).name}",
        "uploadedAt": store.now(),
        "fromImageTask": task.get("id"),
    }
    image["usedBy"] = {
        "canvasId": canvas.get("id"),
        "canvasName": canvas.get("name"),
        "fieldKey": target_key,
        "at": store.now(),
    }
    store.mark_dirty("canvases")
    store.mark_dirty("image_tasks")
    store.save()
    store.flush()
    prev_path = str(prev.get("localPath") or "")
    if prev_path and prev_path != local_path:
        unlink_if_unused(prev_path)
    return {
        "ok": True,
        "canvasId": canvas.get("id"),
        "canvasName": canvas.get("name"),
        "field": field.get("description") or field.get("fieldName") or "",
    }


# ------------------------------------------------------------------ 参考图


@router.post("/api/image-refs")
async def upload_image_ref(request: Request) -> dict:
    buffer = await raw_body_limited(request)
    if not buffer:
        bad_request("没有收到图片内容")
    if len(buffer) > MAX_REF_BYTES:
        bad_request(f"参考图单张不能超过 {MAX_REF_BYTES // (1024 * 1024)}MB")
    original_name = original_name_from(request, "ref.png")
    try:
        ref = create_ref_from_upload(buffer=buffer, original_name=original_name)
    except ValueError as err:
        bad_request(str(err))
    return ref


@router.delete("/api/image-refs/{ref_id}")
async def delete_image_ref(ref_id: str) -> dict:
    ref = find_image_ref(ref_id)
    if not ref:
        not_found("参考图不存在")
    remove_image_ref(ref)
    store.mark_dirty("image_refs")
    store.save()
    store.flush()
    return ok()


# ------------------------------------------------------------------ 导入网页结果


@router.post("/api/image-import")
async def import_web_image(request: Request) -> dict:
    """把「网页生成」落到本地临时目录的图片收编成一条生成记录"""
    body = await json_body(request)
    media = paths()
    root = Path(media.uploads).resolve().parent / "douyin-tmp"
    target = Path(str_or(body.get("localPath"))).resolve()
    # 安全闸：只接受临时目录里的文件，否则等于给了个任意读文件的口子
    if str(target) != str(root) and not str(target).startswith(str(root) + str(Path.sep)):
        bad_request("本地文件不在允许的临时目录里，已拒绝")
    if not target.is_file():
        bad_request("这张图的本地文件已不在，无法导入")
    size = target.stat().st_size
    if size <= 0:
        bad_request("拿到的文件是空的")
    ext = target.suffix.lower() or ".png"
    if not (2 <= len(ext) - 1 <= 5) or not all(ch.isalnum() for ch in ext[1:]):
        ext = ".png"
    stored_name = f"{next_id('gen')}{ext}"
    dest = Path(media.generated) / stored_name
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(target.read_bytes())

    task = new_image_task(
        model_id="",
        model_name=f"网页 · {str_or(body.get('platform') or 'web')[:40]}",
        prompt=str_or(body.get("prompt"))[:2000],
        size="",
        count=1,
        refs=[],
    )
    now = store.now()
    task["status"] = "success"
    task["startedAt"] = now
    task["finishedAt"] = now
    task["images"] = [
        {
            "id": next_id("gi"),
            "fileName": stored_name,
            "localPath": str(dest),
            "localUrl": f"/files/generated/{stored_name}",
            "remoteUrl": str_or(body.get("sourceUrl")),
            "size": size,
            "usedBy": None,
        }
    ]
    store.image_tasks.insert(0, task)
    store.mark_dirty("image_tasks")
    store.save()
    store.flush()
    return {"ok": True, "taskId": task["id"], "image": task["images"][0]}


# ------------------------------------------------------------------ 本地豆包


@router.get("/api/doubao/status")
async def doubao_status(endpoint: str | None = None) -> dict:
    target = trim(endpoint) or doubao_bridge.DOUBAO_DEFAULT_ENDPOINT
    try:
        info = await doubao_bridge.probe_bridge(target)
    except Exception as err:  # noqa: BLE001
        return {"ok": False, "endpoint": target, "browser": "", "detail": str(err)}
    return {
        "ok": bool(info.get("ok")),
        "endpoint": target,
        "browser": info.get("browser") or "",
        "detail": info.get("detail") or "",
    }


@router.post("/api/doubao/restart")
async def doubao_restart(request: Request) -> dict:
    body = await json_body(request)
    target = trim(body.get("endpoint")) or doubao_bridge.DOUBAO_DEFAULT_ENDPOINT
    try:
        result = await doubao_bridge.restart_doubao(target)
    except Exception as err:  # noqa: BLE001
        bad_request(str(err))
    if not result.get("ok"):
        fail(502, result.get("detail") or "豆包重启失败", **result)
    return result


@router.get("/api/doubao/accounts")
async def doubao_accounts(endpoint: str | None = None) -> dict:
    target = trim(endpoint) or doubao_bridge.DOUBAO_DEFAULT_ENDPOINT
    try:
        info = await doubao_bridge.read_doubao_accounts(target)
    except Exception as err:  # noqa: BLE001
        fail(502, str(err))
    if not info.get("ok"):
        message = "豆包没开调试端口" if info.get("reason") == "no-cdp" else "读不到豆包账号菜单"
        fail(502, message, **info)
    return info


@router.post("/api/doubao/switch-account")
async def doubao_switch(request: Request) -> dict:
    body = await json_body(request)
    target = trim(body.get("endpoint")) or doubao_bridge.DOUBAO_DEFAULT_ENDPOINT
    avoid = body.get("avoid") if isinstance(body.get("avoid"), list) else []
    try:
        return await doubao_bridge.switch_doubao_account(
            endpoint=target, to=str_or(body.get("to")), avoid=avoid
        )
    except Exception as err:  # noqa: BLE001
        fail(502, str(err))


__all__ = ["router", "apply_image_model_body", "IMAGE_PROTOCOLS", "DEFAULT_BASE"]
