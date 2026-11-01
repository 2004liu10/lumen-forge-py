"""FastAPI 应用入口 —— 复刻 `server/index.js:2387-2417` 的 `startServer()`。

启动顺序（每一步都不能提前，原版也是这个顺序）：

1. `store.migrate_legacy()` —— 空库时把原版 `db.json` 导进来（有数据就跳过）
2. `Paths(store)` + `scheduler.set_paths()` —— **必须先注入**，调度器里到处
   通过 `get_paths()` 拿目录；没注入就调调度逻辑会直接 RuntimeError
3. `seed()` —— 内置模型 / 素材分类，只在首次或版本号落后时补
4. `migrate_tag_model_to_story()` —— 把「抖音标签」那套模型配置搬给分镜
5. `ensure_providers()` —— 网页平台声明式配置
6. 建 app、挂路由、挂 `/files` 静态
7. 起调度器 / 生图队列 / 生视频队列 / 余额轮询

第 7 步放在 FastAPI 的 `lifespan` 里而不是模块顶层，是为了：
· 端口绑定成功之后才起后台循环（端口被占时不会留下几个跑着的死循环）
· `uvicorn --reload` 每次重载都能干净地停掉旧循环
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .api import (
    accounts,
    canvas_ai,
    canvases,
    image_tasks,
    materials_api,
    node_canvas_api,
    publish_api,
    script_engine_api,
    state,
    storage_api,
    story_api,
    tasks_api,
    topaz_api,
    topics_api,
    video_api,
    web,
)
from .api.deps import HttpError, error_response
from .config import DATA_DIR, PUBLIC_DIR
from .image_jobs import start_image_worker, stop_image_worker
from .materials import set_material_local_base
from .paths import Paths, repair_moved_local_paths
from .scheduler import get_paths, set_paths, start_scheduler, stop_scheduler
from .seed import seed
from .store import store
from .storyboard import migrate_tag_model_to_story
from .video_jobs import start_video_worker, stop_video_worker
from .web_providers import ensure_providers

log = logging.getLogger(__name__)

#: 所有路由模块。顺序无关紧要（没有互相覆盖的路径），列在这里方便核对。
ROUTERS = [
    state,
    accounts,
    web,
    canvases,
    canvas_ai,
    image_tasks,
    video_api,
    story_api,
    script_engine_api,
    tasks_api,
    topaz_api,
    storage_api,
    materials_api,
    topics_api,
    publish_api,
    node_canvas_api,
]


# ============================================================ /files 静态服务


def _serve(prefix_dirs: list[Path], rel: str) -> Response:
    """按前缀的目录清单依次找文件，找到就回，全没有就 404。

    对应原版 `serveFiles()`：`express.static` 创建时会把目录钉死，所以那边是
    「目录一变就重建实例」。这里没有缓存问题 —— 每次请求现算。
    多目录（画质提升结果 = 当前目录 + 旧目录）时依次试，跟原版行为一致。
    """
    # 路径穿越闸：`..` 一律拒，绝对路径也拒
    if rel.startswith(("/", "\\")) or ".." in rel.replace("\\", "/").split("/"):
        return Response(status_code=404)
    for directory in prefix_dirs:
        if not directory:
            continue
        target = Path(directory) / rel
        try:
            if target.is_file():
                # FileResponse 自带 Range 支持（视频缩略图 / 拖动进度都靠它）
                return FileResponse(target)
        except OSError:
            continue
    return Response(status_code=404)


def _prefix_dirs(prefix: str) -> list[Path]:
    """某个虚拟前缀当前对应哪些物理目录"""
    paths = get_paths()
    if prefix == "uploads":
        return [paths.uploads]
    if prefix == "outputs":
        return [paths.outputs]
    if prefix == "topaz":
        # 改过位置之后，旧目录还要继续服务，否则历史成品全 404
        return [paths.topaz, *paths.topaz_legacy_dirs()]
    if prefix == "generated/refs":
        return [paths.refs]
    if prefix == "generated":
        return [paths.generated]
    if prefix == "videos":
        return [paths.videos]
    if prefix == "video-inputs":
        return [paths.video_inputs]
    if prefix == "materials":
        return [paths.materials]
    return []


#: 前缀清单。`generated/refs` 必须排在 `generated` 前面 ——
#: 参考图能被单独搬走，落进 generated 的路由就再也找不到了。
FILE_PREFIXES = [
    "uploads",
    "outputs",
    "topaz",
    "generated/refs",
    "generated",
    "videos",
    "video-inputs",
    "materials",
]


# ============================================================ 生命周期


def bootstrap() -> None:
    """启动前的数据准备（纯同步，不依赖事件循环）"""
    # 1. 老库迁移（空库才跑）
    try:
        stats = store.migrate_legacy()
        if stats:
            log.info("[bootstrap] 已从 db.json 导入：%s", stats)
    except Exception as err:  # noqa: BLE001 - 迁移失败不该让程序起不来
        log.error("[bootstrap] db.json 迁移失败：%s", err)

    # 2. 存储位置必须先注入，后面所有模块都靠它
    paths = Paths(store)
    set_paths(paths)

    # 3. 内置模型 / 素材分类
    try:
        if seed():
            store.flush()
    except Exception as err:  # noqa: BLE001
        log.error("[bootstrap] 播种失败：%s", err)

    # 4. 抖音标签 → 分镜 的模型配置迁移（必须在任何请求之前，否则首屏拿到空配置）
    try:
        migrate_tag_model_to_story()
    except Exception as err:  # noqa: BLE001
        log.error("[bootstrap] 分镜模型迁移失败：%s", err)

    # 5. 网页平台内置配置
    try:
        ensure_providers()
    except Exception as err:  # noqa: BLE001
        log.error("[bootstrap] 网页平台初始化失败：%s", err)

    # 6. 素材模块的本地回环地址（端口要在起服务之后才知道，这里先给个占位）
    set_material_local_base(f"http://127.0.0.1:{os.environ.get('LUMEN_PORT', '8787')}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """起后台循环 / 关后台循环"""
    # 端口已经绑好了，现在才知道真实地址 —— 素材模块要拿它拼本地回环 URL
    port = getattr(app.state, "port", None) or os.environ.get("LUMEN_PORT") or "8787"
    set_material_local_base(f"http://127.0.0.1:{port}")

    start_scheduler()
    start_image_worker()
    start_video_worker()
    # 顺手修一次「存储位置搬过家之后素材路径还指着旧盘」的历史记录
    try:
        repair_moved_local_paths(get_paths())
    except Exception as err:  # noqa: BLE001
        log.warning("[bootstrap] 修复素材路径失败：%s", err)
    log.info("[server] 后台循环已启动")
    try:
        yield
    finally:
        await stop_scheduler()
        await stop_image_worker()
        await stop_video_worker()
        try:
            store.flush()
            store.close()
        except Exception as err:  # noqa: BLE001
            log.warning("[server] 关闭数据库失败：%s", err)
        log.info("[server] 已停止")


# ============================================================ 建 app


def create_app() -> FastAPI:
    app = FastAPI(
        title="Lumen Forge",
        version="0.8.3",
        lifespan=lifespan,
        # 关掉自动文档：这是桌面程序的内部 API，暴露 /docs 没有意义还多一份攻击面
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    # ---------------------------------------------------------- 异常 → {error}
    @app.exception_handler(HttpError)
    async def _http_error(request: Request, exc: HttpError) -> JSONResponse:
        # 原版所有业务错误都是 `res.status(400).json({error: err.message})`，
        # 前端只认 `error` 这个字段名，所以这里必须翻成同样的形状
        return error_response(exc.status, exc.message, exc.extra)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        # 对应原版最后的 500 兜底中间件
        log.exception("[server] 未处理错误 %s", exc)
        return JSONResponse(
            {"error": str(exc) or "服务器内部错误"}, status_code=500
        )

    # ---------------------------------------------------------- 业务路由
    for module in ROUTERS:
        app.include_router(module.router)

    # ---------------------------------------------------------- /files 媒体
    for prefix in FILE_PREFIXES:
        app.add_api_route(
            f"/files/{prefix}/{{rel:path}}",
            _make_file_handler(prefix),
            methods=["GET", "HEAD"],
            include_in_schema=False,
        )
    # 兜底：目录没被改过时它们本来就在 DATA_DIR 下，这一条足够覆盖
    app.add_api_route(
        "/files/{rel:path}",
        _make_file_handler(""),
        methods=["GET", "HEAD"],
        include_in_schema=False,
    )

    # ---------------------------------------------------------- 前端静态
    # `public/` 是原版的前端；Python 版直接复用同一套前端，不用重写
    if PUBLIC_DIR.exists():
        app.mount("/", StaticFiles(directory=str(PUBLIC_DIR), html=True), name="public")
    else:
        log.warning("[server] 没找到前端目录 %s（只提供 API）", PUBLIC_DIR)

    app.state.data_dir = DATA_DIR
    return app


def _make_file_handler(prefix: str):
    """给某个前缀生成处理函数（闭包捕获 prefix）"""

    async def handler(rel: str) -> Response:
        dirs = _prefix_dirs(prefix) if prefix else [DATA_DIR]
        return _serve(dirs, rel)

    return handler


#: `uvicorn app.main:app` 用的实例
app = create_app()


def start_server(*, host: str = "127.0.0.1", port: int = 0) -> dict:
    """同步启动（`run.py` 用）。返回实际监听地址。"""
    import uvicorn

    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="info",
        # 桌面程序里不需要 access log，会跟业务日志混在一起
        access_log=False,
    )
    server = uvicorn.Server(config)
    return {"server": server, "config": config}


__all__ = ["app", "create_app", "bootstrap", "start_server", "ROUTERS"]
