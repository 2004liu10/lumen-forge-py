"""无限画布 + 本地素材库 —— 对应 `server/node-canvas.js:365` 的 `attachNodeCanvasRoutes(app)`。

**这 16 条路由不在 `tools/contract.json` 的 112 条基线里**：原版是
`attachNodeCanvasRoutes(app)` 自己往 app 上挂的，用 `app.get(...)` 注册，
所以早期机械提取（只扫 `router`/`registerXxxRoutes` 风格的模块）漏了它。
补上之后全量是 **129 条**。

两条硬约束：

* `POST /api/node-assets` 走原始二进制（`express.raw({limit:'500mb'})`），
  不引入 multipart —— 文件名从 `x-filename` 头拿，原文件名只进 `originalName` 展示。
* 节点画布本身**没有列表路由**：列表在 `GET /api/state` 的 `nodeCanvases` 里，
  原版 `nodeCanvasSnapshot()` 返回的是数组（不是字典）。
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from ..node_canvas import (
    asset_lists,
    asset_to_image_ref,
    clear_trash,
    create_graph,
    delete_chat,
    delete_graph,
    export_file_name,
    export_graph,
    find_node_asset,
    find_node_canvas,
    import_graph,
    migrate_legacy_chats,
    patch_asset,
    purge_asset,
    register_existing,
    register_upload,
    restore_asset,
    save_chat,
    trash_asset,
    update_graph,
)
from ..store import store
from .deps import (
    HttpError,
    json_body,
    not_found,
    ok,
    original_name_from,
    paths,
    raw_body,
)

log = logging.getLogger(__name__)

router = APIRouter()

#: 上传素材的体积上限，跟原版 `express.raw({ limit: '500mb' })` 对齐
MAX_ASSET_BYTES = 500 * 1024 * 1024


def _require_graph(canvas_id: str) -> dict:
    graph = find_node_canvas(canvas_id)
    if not graph:
        raise HttpError(404, "节点画布不存在")
    return graph


def _require_asset(asset_id: str) -> dict:
    asset = find_node_asset(asset_id)
    if not asset:
        raise HttpError(404, "素材不存在")
    return asset


def _safe(fn, *args):
    """业务层用 `ValueError` / `LookupError` 报错，这里统一翻成 4xx。"""
    try:
        return fn(*args)
    except LookupError as err:
        raise HttpError(404, str(err)) from err
    except ValueError as err:
        raise HttpError(400, str(err)) from err
    except RuntimeError as err:
        raise HttpError(500, str(err)) from err


# ---------------------------------------------------------------- 节点画布


@router.post("/api/node-canvases")
async def create_node_canvas(request: Request) -> dict:
    body = await json_body(request)
    return _safe(create_graph, body.get("name"))


@router.patch("/api/node-canvases/{canvas_id}")
async def patch_node_canvas(canvas_id: str, request: Request) -> dict:
    graph = _require_graph(canvas_id)
    body = await json_body(request)
    updated = _safe(update_graph, graph, body)
    return {"ok": True, "updatedAt": updated.get("updatedAt")}


@router.delete("/api/node-canvases/{canvas_id}")
async def remove_node_canvas(canvas_id: str) -> dict:
    graph = _require_graph(canvas_id)
    delete_graph(graph)
    return {"ok": True}


@router.post("/api/node-canvases/{canvas_id}/chat")
async def post_node_canvas_chat(canvas_id: str, request: Request) -> dict:
    graph = _require_graph(canvas_id)
    body = await json_body(request)
    return _safe(save_chat, graph, body)


@router.delete("/api/node-canvases/{canvas_id}/chat/{chat_id}")
async def delete_node_canvas_chat(canvas_id: str, chat_id: str) -> dict:
    graph = _require_graph(canvas_id)
    return _safe(delete_chat, graph, chat_id)


@router.get("/api/node-canvases/{canvas_id}/export")
async def export_node_canvas(canvas_id: str) -> Response:
    graph = _require_graph(canvas_id)
    payload = export_graph(graph)
    file_name = export_file_name(graph)
    # 原版：`Content-Disposition: attachment; filename*=UTF-8''<urlencoded>`
    quoted = _quote_rfc5987(file_name)
    return JSONResponse(
        content=payload,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Content-Disposition": f"attachment; filename*=UTF-8''{quoted}",
        },
    )


@router.post("/api/node-canvases/import")
async def import_node_canvas(request: Request) -> dict:
    body = await json_body(request)
    # 允许直接贴一份画布 JSON，也允许包一层 `{"graph": {...}}`
    raw = body.get("graph") if body.get("graph") is not None else body
    if isinstance(body, dict) and body.get("__array__") is not None:
        raise HttpError(400, "文件内容不是有效的画布 JSON")
    return _safe(import_graph, raw)


# ---------------------------------------------------------------- 本地素材库


@router.post("/api/node-assets")
async def upload_node_asset(request: Request) -> dict:
    buffer = await raw_body(request, limit=MAX_ASSET_BYTES)
    if not buffer:
        raise HttpError(400, "没有收到文件内容")
    original_name = original_name_from(request, "素材")
    mime = request.headers.get("x-mime") or ""
    return _safe(register_upload, buffer, original_name, mime, paths())


@router.post("/api/node-assets/register")
async def register_node_asset(request: Request) -> dict:
    body = await json_body(request)
    return _safe(register_existing, body, paths())


@router.post("/api/node-assets/{asset_id}/to-image-ref")
async def node_asset_to_image_ref(asset_id: str) -> dict:
    asset = _require_asset(asset_id)
    return _safe(asset_to_image_ref, asset)


@router.get("/api/node-assets")
async def list_node_assets() -> dict:
    return asset_lists()


@router.patch("/api/node-assets/{asset_id}")
async def patch_node_asset(asset_id: str, request: Request) -> dict:
    asset = _require_asset(asset_id)
    body = await json_body(request)
    return _safe(patch_asset, asset, body)


@router.delete("/api/node-assets/{asset_id}")
async def delete_node_asset(asset_id: str) -> dict:
    asset = _require_asset(asset_id)
    return _safe(trash_asset, asset)


@router.post("/api/node-assets/{asset_id}/restore")
async def restore_node_asset(asset_id: str) -> dict:
    asset = _require_asset(asset_id)
    return _safe(restore_asset, asset)


@router.delete("/api/node-assets/{asset_id}/purge")
async def purge_node_asset(asset_id: str) -> dict:
    asset = _require_asset(asset_id)
    return _safe(purge_asset, asset)


@router.post("/api/node-assets/clear-trash")
async def clear_node_asset_trash() -> dict:
    return _safe(clear_trash)


# ------------------------------------------------------------------ 小工具


def _quote_rfc5987(value: str) -> str:
    """RFC 5987 的 `filename*=UTF-8''...` 编码。

    `urllib.parse.quote` 默认保留 `/`，文件名里不该出现它，顺手一起编码掉，
    免得 `a/b.json` 变成两级目录。
    """
    from urllib.parse import quote

    return quote(str(value or ""), safe="")


def startup() -> None:
    """开机先把老存档的 `graph.chat` 升级成 `chats[]`。

    原版这段写在 `attachNodeCanvasRoutes()` 开头，只在内存里改、有改动才落盘一次。
    这里由 `main.bootstrap()` 调用 —— 挂在 import 时会跑在任何东西准备好之前。
    """
    if migrate_legacy_chats():
        store.save()
        store.flush()


__all__ = ["router", "startup", "MAX_ASSET_BYTES"]
