"""视频模型、输入图与视频任务 —— 对应 `server/index.js:1799-1955`。

视频这边比图片多一层「输入图」：首帧 / 尾帧是独立登记的素材，可以被多个任务复用。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import APIRouter, Request

from ..ids import next_id
from ..models import new_video_model
from ..providers import videogen
from ..shared_files import unlink_if_unused
from ..store import store
from ..video_jobs import (
    cancel_video_task,
    create_video_input_from_image,
    create_video_input_from_upload,
    create_video_task,
    find_video_input,
    find_video_model,
    find_video_task,
    remove_video_input,
    retry_video_task,
    video_input_snapshot,
    video_model_snapshot,
)
from .deps import (
    as_int,
    bad_request,
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


def apply_video_model_body(model: dict, body: dict) -> dict:
    """把请求体套到视频模型上。抛 `ValueError` 由路由翻成 400。"""
    provider = body.get("provider")
    if provider is not None:
        provider = trim(provider)
        if provider not in videogen.PROVIDER_PRESETS:
            allowed = " / ".join(videogen.PROVIDER_PRESETS.keys())
            raise ValueError(f"服务商只支持 {allowed}")
        changed = provider != model.get("provider")
        model["provider"] = provider
        if changed and not trim(body.get("baseUrl")):
            model["baseUrl"] = (videogen.PROVIDER_PRESETS.get(provider) or {}).get("baseUrl") or ""

    if body.get("name") is not None and trim(body.get("name")):
        model["name"] = trim(body.get("name"))
    if "baseUrl" in body:
        model["baseUrl"] = trim(body.get("baseUrl"))
    # 密钥只在非空时覆盖（前端会把掩码回显回来）
    if trim(body.get("apiKey")):
        model["apiKey"] = trim(body.get("apiKey"))
    for key in ("model", "defaultDuration", "defaultResolution", "defaultRatio"):
        if key in body:
            model[key] = trim(body.get(key))
    if "enabled" in body:
        model["enabled"] = bool(body.get("enabled"))
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


@router.post("/api/video-models")
async def create_video_model(request: Request) -> dict:
    body = await json_body(request)
    if not trim(body.get("name")):
        bad_request("请填写模型名称")
    model = new_video_model({"name": trim(body.get("name"))})
    try:
        apply_video_model_body(model, body)
    except ValueError as err:
        bad_request(str(err))
    store.video_models.append(model)
    store.mark_dirty("video_models")
    store.save()
    store.flush()
    return video_model_snapshot(model)


@router.patch("/api/video-models/{model_id}")
async def patch_video_model(model_id: str, request: Request) -> dict:
    model = find_video_model(model_id)
    if not model:
        not_found("视频模型不存在")
    body = await json_body(request)
    try:
        apply_video_model_body(model, body)
    except ValueError as err:
        bad_request(str(err))
    store.mark_dirty("video_models")
    store.save()
    store.flush()
    return video_model_snapshot(model)


@router.delete("/api/video-models/{model_id}")
async def delete_video_model(model_id: str) -> dict:
    if not find_video_model(model_id):
        not_found("视频模型不存在")
    store.video_models = [m for m in store.video_models if m.get("id") != model_id]
    store.mark_dirty("video_models")
    store.save()
    store.flush()
    return ok()


# ------------------------------------------------------------------ 输入图


@router.post("/api/video-inputs")
async def upload_video_input(request: Request) -> dict:
    buffer = await raw_body(request, limit=40 * 1024 * 1024)
    if not buffer:
        bad_request("没有收到图片内容")
    original_name = original_name_from(request, "input.png")
    try:
        return create_video_input_from_upload(buffer=buffer, original_name=original_name)
    except ValueError as err:
        bad_request(str(err))


@router.delete("/api/video-inputs/{input_id}")
async def delete_video_input(input_id: str) -> dict:
    input_ = find_video_input(input_id)
    if not input_:
        not_found("输入图不存在")
    remove_video_input(input_)
    store.mark_dirty("video_inputs")
    store.save()
    store.flush()
    return ok()


@router.post("/api/image-tasks/{task_id}/to-video")
async def image_to_video_input(task_id: str, request: Request) -> dict:
    """把一张生成图转成视频输入图（首帧 / 尾帧），同一张图可被多个视频任务复用"""
    task = find_image_task_or_404(task_id)
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
        input_ = create_video_input_from_image(task, image)
    except ValueError as err:
        bad_request(str(err))
    image["sentToVideo"] = {"inputId": input_.get("id"), "at": store.now()}
    store.mark_dirty("image_tasks")
    store.save()
    store.flush()
    return input_


def find_image_task_or_404(task_id: str) -> dict:
    from ..image_jobs import find_image_task

    task = find_image_task(task_id)
    if not task:
        not_found("生成记录不存在")
    return task


# ------------------------------------------------------------------ 视频任务


@router.post("/api/video-tasks")
async def post_video_task(request: Request) -> dict:
    body = await json_body(request)
    try:
        task = create_video_task(
            model_id=str_or(body.get("modelId")) or None,
            prompt=str_or(body.get("prompt")),
            duration=body.get("duration"),
            ratio=body.get("ratio"),
            resolution=body.get("resolution"),
            seed=body.get("seed"),
            tier=body.get("tier"),
            input_id=str_or(body.get("inputId")) or None,
            end_input_id=str_or(body.get("endInputId")) or None,
        )
    except ValueError as err:
        bad_request(str(err))
    return task


@router.post("/api/video-tasks/{task_id}/retry")
async def retry_video(task_id: str) -> dict:
    task = find_video_task(task_id)
    if not task:
        not_found("视频任务不存在")
    try:
        return retry_video_task(task)
    except ValueError as err:
        bad_request(str(err))


@router.post("/api/video-tasks/{task_id}/cancel")
async def cancel_video(task_id: str) -> dict:
    task = find_video_task(task_id)
    if not task:
        not_found("视频任务不存在")
    try:
        return cancel_video_task(task)
    except ValueError as err:
        bad_request(str(err))


@router.delete("/api/video-tasks/{task_id}")
async def delete_video_task(task_id: str) -> dict:
    task = find_video_task(task_id)
    if not task:
        not_found("视频任务不存在")
    store.video_tasks = [t for t in store.video_tasks if t.get("id") != task_id]
    store.mark_dirty("video_tasks")
    store.save()
    store.flush()
    video_path = str((task.get("video") or {}).get("localPath") or "")
    if video_path:
        unlink_if_unused(video_path)
    return ok()


__all__ = ["router", "apply_video_model_body"]
