"""素材 —— 分类配置、线路池、拉列表、下载、抓图源。对应 `server/index.js:1355-1654`。

「素材分类」是用户配的一条取素材线路（m3u8 / api / redirect / 抖音采集），
「线路池」是多个备用域名（应对某个域名被墙）。下载下来的文件进 `materials/`。
"""

from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, Request
from fastapi.responses import Response

from ..canvas_params import field_label
from ..config import MAX_REF_BYTES
from ..ids import next_id
from ..image_jobs import create_ref_from_existing_file
from ..materials import (
    download_material,
    fetch_material_items,
    fetch_ranking,
    find_material_source,
    import_local_material,
    invalidate_all_material_items,
    invalidate_material_items,
    is_clip_id,
    material_headers,
    material_source_snapshot,
    random_images,
    read_material_snapshot,
    search_images,
)
from ..models import new_material_source, normalize_line_pool, normalize_material_lines
from ..scheduler import field_key
from ..shared_files import share_file, unlink_if_unused
from ..store import store
from ..topics import add_topics
from .canvases import MIME_BY_EXT, find_canvas
from .deps import (
    HttpError,
    as_int,
    bad_request,
    json_body,
    not_found,
    ok,
    paths,
    raw_body,
    str_or,
    trim,
)

log = logging.getLogger(__name__)

router = APIRouter()


def apply_material_source_body(source: dict, body: dict) -> dict:
    """把请求体套到素材分类上。抛 `ValueError` 由路由翻成 400。"""
    if body.get("name") is not None and trim(body.get("name")):
        source["name"] = trim(body.get("name"))
    if "kind" in body:
        source["kind"] = "image" if body.get("kind") == "image" else "video"

    # 抖音采集分类的 mode 是固定的：它的取素材逻辑完全不同于 m3u8/api/redirect
    if source.get("mode") != "douyin" and "mode" in body:
        source["mode"] = body.get("mode") if body.get("mode") in ("m3u8", "redirect") else "api"

    if "lines" in body:
        source["lines"] = normalize_material_lines(body.get("lines"))
    if "clip" in body:
        clip = trim(body.get("clip"))
        if clip and not is_clip_id(clip):
            raise ValueError("编号只能用字母、数字和 - _ .（要拼进地址）")
        source["clip"] = clip
    if "count" in body:
        source["count"] = max(1, min(48, as_int(body.get("count"), 8) or 8))
    if "headers" in body:
        source["headers"] = str_or(body.get("headers"))
    # 密钥同图片模型：掩码回显不能覆盖真 Key
    api_key = str_or(body.get("apiKey"))
    if api_key and "****" not in api_key:
        source["apiKey"] = api_key
    if "apiKeyHeader" in body:
        source["apiKeyHeader"] = trim(body.get("apiKeyHeader")) or "Authorization"
    if "apiKeyPrefix" in body:
        source["apiKeyPrefix"] = str_or(body.get("apiKeyPrefix"))
    for key in ("itemsPath", "urlField"):
        if key in body:
            source[key] = trim(body.get(key))
    if "extraParams" in body:
        source["extraParams"] = str_or(body.get("extraParams"))
    if "enabled" in body:
        source["enabled"] = bool(body.get("enabled"))
    return source


# ------------------------------------------------------------------ 分类


@router.post("/api/material-sources")
async def create_material_source(request: Request) -> dict:
    body = await json_body(request)
    if not trim(body.get("name")):
        bad_request("请填写分类名称")
    source = new_material_source({"name": trim(body.get("name"))})
    try:
        apply_material_source_body(source, body)
    except ValueError as err:
        bad_request(str(err))
    store.material_sources.append(source)
    store.mark_dirty("material_sources")
    store.save()
    store.flush()
    return material_source_snapshot(source)


@router.patch("/api/material-sources/{source_id}")
async def patch_material_source(source_id: str, request: Request) -> dict:
    source = find_material_source(source_id)
    if not source:
        not_found("素材分类不存在")
    body = await json_body(request)
    try:
        apply_material_source_body(source, body)
    except ValueError as err:
        bad_request(str(err))
    invalidate_material_items(source_id)
    store.mark_dirty("material_sources")
    store.save()
    store.flush()
    return material_source_snapshot(source)


@router.delete("/api/material-sources/{source_id}")
async def delete_material_source(source_id: str) -> dict:
    if not find_material_source(source_id):
        not_found("素材分类不存在")
    store.material_sources = [s for s in store.material_sources if s.get("id") != source_id]
    invalidate_material_items(source_id)
    store.mark_dirty("material_sources")
    store.save()
    store.flush()
    return ok()


# ------------------------------------------------------------------ 线路池


@router.put("/api/material-line-pool")
async def put_line_pool(request: Request) -> dict:
    """线路池是「一堆域名根」，拼成 `https://a.example/video/1` 这种地址去轮换"""
    body = await json_body(request)
    raw = body.get("__array__") if isinstance(body, dict) and "__array__" in body else body.get("lines")
    pool = normalize_line_pool(raw if raw is not None else body)
    for line in pool:
        try:
            parsed = urlparse(line)
        except ValueError:
            bad_request(f"线路基地址不合法：{line}（要 http(s) 开头的完整域名，不带编号）")
        if parsed.scheme not in ("http", "https"):
            bad_request(f"线路基地址必须以 http(s) 开头：{line}")
        if parsed.path.replace("/", "").strip():
            bad_request(f"线路基地址只填域名根，不要带路径：{line}")
    store.material_line_pool = pool
    invalidate_all_material_items()
    store.mark_dirty("material_line_pool")
    store.save()
    store.flush()
    return {"lines": pool}


# ------------------------------------------------------------------ 取素材


@router.get("/api/material-sources/{source_id}/items")
async def list_material_items(source_id: str, refresh: str | None = None) -> dict:
    source = find_material_source(source_id)
    if not source:
        not_found("素材分类不存在")
    try:
        out = fetch_material_items(source, refresh=(refresh == "1"))
    except Exception as err:  # noqa: BLE001 - 上游线路千奇百怪，报错信息原样给用户
        bad_request(str(err))
    return {"items": out.get("items") or [], "line": out.get("line") or "", "fetchedAt": store.now()}


@router.get("/api/materials/rand/{token}")
async def material_random_file(token: str) -> Response:
    """把「这一批随机素材」里的某一张/某一段吐出来（前端直接当图片/视频地址用）"""
    snap = read_material_snapshot(token)
    if not snap:
        not_found("这一份随机素材已过期，点「刷新」重新拉一批")
    return Response(
        content=snap["buffer"],
        media_type=snap.get("mime") or "application/octet-stream",
        headers={"Cache-Control": "public, max-age=1800"},
    )


async def _material_call(fn, **kwargs) -> dict:
    """图源现搜是网络请求，业务层用 `ValueError` 报「没搜到」，这里翻成 400。

    原版这几条路由都是 `try { ... } catch (err) { res.status(400).json(...) }`，
    漏了这层包裹的话 FastAPI 会把它当未处理异常直接回 500。

    必须 `await`：`random_images` / `search_images` / `fetch_ranking` 都是
    `async def`，不 await 的话异常根本不会在这里抛出来。
    """
    try:
        return await fn(**kwargs)
    except ValueError as err:
        raise HttpError(400, str(err)) from err


@router.get("/api/materials/random")
async def material_random(page: int = 0, limit: int = 16) -> dict:
    return await _material_call(random_images, page=page, limit=limit)


@router.get("/api/materials/search")
async def material_search(
    q: str = "", source: str = "pixiv", page: int = 0, limit: int = 24
) -> dict:
    return await _material_call(search_images, q=q, source=source or "pixiv", page=page, limit=limit)


@router.get("/api/materials/ranking")
async def material_ranking(mode: str = "day", page: int = 0, limit: int = 24) -> dict:
    return await _material_call(fetch_ranking, mode=mode or "day", page=page, limit=limit)


@router.post("/api/materials/download")
async def material_download(request: Request) -> dict:
    body = await json_body(request)
    kind = "image" if body.get("kind") == "image" else "video"
    source = find_material_source(str_or(body.get("sourceId"))) if body.get("sourceId") else None
    headers = material_headers(source) if source else {}
    try:
        out = download_material(
            str_or(body.get("url")),
            kind=kind,
            headers=headers,
            unique=bool(body.get("unique")),
        )
    except Exception as err:  # noqa: BLE001
        bad_request(str(err))
    return {"fileName": out.get("fileName"), "localUrl": out.get("localUrl"), "size": out.get("size")}


@router.post("/api/materials/to-canvas")
async def material_to_canvas(request: Request) -> dict:
    """把素材（下载或本地文件）挂到画布的视频参数上，并顺手把带的抖音话题收进话题库"""
    body = await json_body(request)
    canvas = find_canvas(str_or(body.get("canvasId"))) if body.get("canvasId") else None
    if not canvas:
        bad_request("请选择要添加到的画布")
    fields = canvas.get("fields") or []
    field = None
    if body.get("fieldKey"):
        field = next((f for f in fields if field_key(f) == body["fieldKey"]), None)
    else:
        field = next((f for f in fields if str(f.get("fieldType") or "").upper() == "VIDEO"), None)
    if not field:
        bad_request(f"画布「{canvas.get('name')}」还没有视频输入参数，请先在该画布点「读取参数」")
    if str(field.get("fieldType") or "").upper() != "VIDEO":
        bad_request("选中的参数不是视频类型，请选「加载参考视频」这类参数")

    if body.get("localFile"):
        file = import_local_material(str_or(body.get("localFile")), "video")
    else:
        source = find_material_source(str_or(body.get("sourceId"))) if body.get("sourceId") else None
        file = download_material(
            str_or(body.get("url")),
            kind="video",
            headers=material_headers(source) if source else {},
            unique=bool(body.get("unique")),
        )
    local_path = str(Path(str(file.get("localPath") or "")).resolve())
    ext = Path(local_path).suffix.lower() or ".mp4"
    media = paths()
    prefix = "/files/materials/" if local_path.startswith(str(media.materials)) else "/files/uploads/"
    target_key = field_key(field)
    prev = (canvas.get("assets") or {}).get(target_key) or {}
    shared = share_file(
        local_path=local_path,
        original_name=str(file.get("fileName") or Path(local_path).name),
        mime_type=MIME_BY_EXT.get(ext.lstrip("."), "video/mp4"),
        size=as_int(file.get("size"), 0),
        origin="material",
    )
    canvas.setdefault("assets", {})[target_key] = {
        **shared,
        "localUrl": f"{prefix}{Path(local_path).name}",
        "uploadedAt": store.now(),
        "fromMaterial": True,
        "topics": [str(t).lstrip("#").strip()[:12] for t in (body.get("topics") or [])]
        if isinstance(body.get("topics"), list)
        else [],
        "sourceDesc": str_or(body.get("sourceDesc"))[:80],
    }
    incoming = canvas["assets"][target_key]["topics"]
    topic_result = {"added": 0, "existed": 0}
    if incoming:
        topic_result = add_topics(
            incoming,
            source="douyin",
            source_desc=canvas["assets"][target_key]["sourceDesc"] or str(file.get("fileName") or ""),
        )
    store.mark_dirty("canvases")
    store.save()
    store.flush()
    prev_path = str(prev.get("localPath") or "")
    if prev_path and prev_path != local_path:
        unlink_if_unused(prev_path)
    return {
        "ok": True,
        "canvasId": canvas.get("id"),
        "canvasName": canvas.get("name"),
        "field": field_label(field=field, value="") or field.get("description") or field.get("fieldName") or "",
        "topicsAdded": topic_result.get("added") or 0,
        "topicsExisted": topic_result.get("existed") or 0,
    }


@router.post("/api/materials/to-image-ref")
async def material_to_image_ref(request: Request) -> dict:
    body = await json_body(request)
    if body.get("localFile"):
        file = import_local_material(str_or(body.get("localFile")), "image")
    else:
        source = find_material_source(str_or(body.get("sourceId"))) if body.get("sourceId") else None
        file = download_material(
            str_or(body.get("url")),
            kind="image",
            headers=material_headers(source) if source else {},
            referer=str_or(body.get("referer")),
        )
    try:
        ref = create_ref_from_existing_file(
            local_path=str(file.get("localPath") or ""),
            original_name=str(file.get("fileName") or ""),
            mime_type=file.get("mimeType"),
            size=as_int(file.get("size"), 0),
        )
    except ValueError as err:
        bad_request(str(err))
    store.mark_dirty("image_refs")
    store.save()
    store.flush()
    return ref


__all__ = ["router", "apply_material_source_body"]
