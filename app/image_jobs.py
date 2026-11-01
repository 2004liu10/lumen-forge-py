"""生图队列 —— 逐函数复刻 `server/image-jobs.js`（363 行）。

## 它和 `scheduler.py` 是两套完全独立的东西

`scheduler.py` 管 RunningHub 的账号池（多 Key 轮换、额度、并发），
这里管「用户点了生成图片」这件事：直接用某个模型自带的 Key，
**全局串行**（一次只跑一张），没有账号池、没有额度、没有并发概念。

原版是 `setInterval(tick, 1500)` + 全局 `working` 标志；Python 里换成
asyncio 后台任务 + 重入保护，语义完全一致。

## 参考图的三种来源（这层最容易写错）

1. **本机文件**（`localPath` 有值）：上传的图、生成的图、素材图，都要读盘转 data URI。
2. **公网链接**（`url` 有值、`localPath` 为空）：不落库、不复制，直接交给上游模型拉。
3. **任务快照**（`task.refs` 里存的那份）：任务排队时参考图可能被用户从图库删掉，
   所以快照必须能独立还原 —— `reuse_image_refs` 就是干这个的，见那里的注释。
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

from .config import MAX_IMAGE_REFS, MAX_REFS
from .ids import next_id, now_ms
from .models import new_image_ref, new_image_task
from .paths import Paths
from .providers import imagegen
from .scheduler import get_paths, mask_key
from .shared_files import unlink_if_unused
from .store import store

log = logging.getLogger(__name__)

TICK_MS = 1500
DEFAULT_COUNT = 1
MAX_COUNT = 4

#: 上传参考图允许的扩展名 → MIME
REF_MIME_TYPES = imagegen.REF_MIME_TYPES
LINK_RE = re.compile(r"^https?://\S+$")


# ============================================================ 查询 / 快照


def find_image_model(model_id: str | None) -> dict | None:
    if not model_id:
        return None
    return store.find("image_models", id=model_id)


def find_image_task(task_id: str | None) -> dict | None:
    if not task_id:
        return None
    return store.find("image_tasks", id=task_id)


def find_image_ref(ref_id: str | None) -> dict | None:
    if not ref_id:
        return None
    return store.find("image_refs", id=ref_id)


def image_model_snapshot(model: dict) -> dict:
    return {
        **model,
        "apiKey": mask_key(model.get("apiKey")),
        "hasKey": bool(model.get("apiKey")),
        "akSecret": mask_key(model.get("akSecret")),
        "hasSecret": bool(model.get("akSecret")),
    }


def image_ref_snapshot(ref: dict) -> dict:
    return {
        "id": ref.get("id"),
        "originalName": ref.get("originalName") or "",
        "fileName": ref.get("fileName") or "",
        "localUrl": ref.get("localUrl") or "",
        "url": ref.get("url") or "",
        "mimeType": ref.get("mimeType") or "",
        "size": ref.get("size") or 0,
        "source": ref.get("source") or "",
        "shared": bool(ref.get("shared")),
        "createdAt": ref.get("createdAt"),
    }


def model_supports_image_input(model: dict | None) -> bool:
    return imagegen.model_supports_image_input(model or {})


# ============================================================ 参考图登记


def _ref_dir() -> Path:
    return get_paths().refs


def register_ref(
    *,
    buffer: bytes,
    original_name: str,
    mime_type: str | None = None,
    ext: str | None = None,
    source: str | None = None,
) -> dict:
    """把一份图片字节写成参考图文件并登记进图库"""
    if ext is None:
        ext = Path(original_name or "").suffix.lower() or ".png"
    if not ext.startswith("."):
        ext = "." + ext
    file_name = f"{next_id('ref')}{ext}"
    dest = _ref_dir() / file_name
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(buffer)

    ref = new_image_ref(
        original_name=original_name or file_name,
        file_name=file_name,
        local_path=str(dest),
        local_url=f"/files/generated/refs/{file_name}",
        mime_type=mime_type or REF_MIME_TYPES.get(ext, ""),
        size=len(buffer),
        source=source,
    )
    store.image_refs.insert(0, ref)
    # 图库只留最近 MAX_IMAGE_REFS 张，老的自然淘汰（文件由清理逻辑处理）
    if len(store.image_refs) > MAX_IMAGE_REFS:
        del store.image_refs[MAX_IMAGE_REFS:]
    store.mark_dirty("image_refs")
    store.save()
    return ref


def create_ref_from_upload(*, buffer: bytes, original_name: str) -> dict:
    ext = Path(original_name or "").suffix.lower()
    mime = REF_MIME_TYPES.get(ext)
    if not mime:
        raise ValueError(f"参考图只支持 {'/'.join(e.lstrip('.') for e in REF_MIME_TYPES)} 格式")
    return register_ref(buffer=buffer, original_name=original_name, mime_type=mime, ext=ext, source="upload")


def create_ref_from_image(task: dict, image: dict) -> dict:
    """把某张生成结果变成参考图（图生图的常见用法：拿上一张当参考）"""
    local_path = str(image.get("localPath") or "")
    if not local_path or not Path(local_path).exists():
        raise ValueError("这张图片的本地文件已被删除，无法用作参考图")
    path = Path(local_path)
    ext = path.suffix.lower() or ".png"
    return register_ref(
        buffer=path.read_bytes(),
        original_name=str(image.get("fileName") or path.name),
        mime_type=REF_MIME_TYPES.get(ext, ""),
        ext=ext,
        source="generated",
    )


def create_ref_from_buffer(*, buffer: bytes, original_name: str) -> dict:
    return register_ref(buffer=buffer, original_name=original_name, source="material")


def create_ref_from_existing_file(
    *, local_path: str, original_name: str, mime_type: str | None = None, size: int | None = None
) -> dict:
    """直接引用一个已存在的文件，**不复制**。

    素材库里的图已经落在 materials/ 下了，再复制一份纯属浪费磁盘；
    这里只登记路径，`localUrl` 按文件实际所在目录生成。
    """
    path = Path(local_path)
    if not path.exists():
        raise ValueError("素材文件已不存在，无法用作参考图")
    name = path.name
    in_materials = "materials" in path.parts
    ref = new_image_ref(
        original_name=original_name or name,
        file_name=name,
        local_path=str(path),
        local_url=f"/files/materials/{name}" if in_materials else f"/files/generated/refs/{name}",
        mime_type=mime_type or REF_MIME_TYPES.get(path.suffix.lower(), ""),
        size=size if size is not None else path.stat().st_size,
        source="material",
    )
    ref["shared"] = True
    store.image_refs.insert(0, ref)
    store.mark_dirty("image_refs")
    store.save()
    return ref


def create_ref_from_link(url: str) -> dict:
    """公网链接参考图：不落库、不下载，返回一个临时对象"""
    link = str(url or "").strip()
    if not LINK_RE.match(link):
        raise ValueError("参考图链接必须是 http(s) 开头的公网地址")
    return {
        "id": next_id("link"),
        "originalName": link.rsplit("/", 1)[-1] or "link",
        "fileName": "",
        "localPath": "",
        "localUrl": link,
        "mimeType": "",
        "size": 0,
        "url": link,
        "source": "link",
        "createdAt": now_ms(),
    }


def remove_image_ref(ref: dict) -> None:
    refs = store.image_refs
    for index, item in enumerate(refs):
        if item.get("id") == ref.get("id"):
            del refs[index]
            break
    # 统一走共享文件的引用计数：这张参考图可能同时被画布 / 视频输入 / 分镜角色引用着
    unlink_if_unused(ref.get("localPath"))
    store.mark_dirty("image_refs")
    store.save()
    store.flush()


def reuse_image_refs(task: dict) -> list[dict]:
    """把任务快照里的参考图还原成「现在还能用」的参考图对象。

    三种情况都得能还原，否则用户点「复用参数」重生成会直接报「参考图已失效」：
    - 快照里带 `url` 的（链接图）：直接还原成临时对象，根本不需要图库。
    - 图库里还在的：直接复用。
    - 图库被用户删了、但本地文件还在的：**用原来的 id** 重新登记回图库。
      沿用原 id 是关键 —— 否则同一个任务多点几次「复用」就会把同一张图反复登记好几份。
    """
    out: list[dict] = []
    for snap in task.get("refs") or []:
        if snap.get("url"):
            out.append(
                {
                    "id": snap.get("id") or next_id("link"),
                    "originalName": snap.get("originalName") or "",
                    "fileName": snap.get("fileName") or "",
                    "localPath": "",
                    "localUrl": snap.get("url"),
                    "mimeType": snap.get("mimeType") or "",
                    "size": snap.get("size") or 0,
                    "url": snap.get("url"),
                    "source": "link",
                    "createdAt": now_ms(),
                }
            )
            continue

        live = find_image_ref(snap.get("id"))
        if live:
            out.append(live)
            continue

        local_path = str(snap.get("localPath") or "")
        if not local_path or not Path(local_path).exists():
            continue
        path = Path(local_path)
        ext = path.suffix.lower() or ".png"
        ref = new_image_ref(
            original_name=snap.get("originalName") or path.name,
            file_name=path.name,
            local_path=local_path,
            local_url=snap.get("localUrl") or f"/files/generated/refs/{path.name}",
            mime_type=snap.get("mimeType") or REF_MIME_TYPES.get(ext, ""),
            size=int(snap.get("size") or path.stat().st_size),
            source=snap.get("source") or "generated",
        )
        ref["id"] = snap.get("id") or ref["id"]  # 沿用原 id，重复复用不会重复登记
        store.image_refs.insert(0, ref)
        store.mark_dirty("image_refs")
        out.append(ref)
    store.save()
    return out


def pick_refs(items: list | None) -> list[dict]:
    """把请求里的 refIds 解析成参考图对象：`{url}` 走链接，其余按 id 查图库"""
    out: list[dict] = []
    for item in items or []:
        if len(out) >= MAX_REFS:
            break
        if isinstance(item, dict):
            if item.get("url"):
                out.append(create_ref_from_link(str(item["url"])))
            continue
        ref = find_image_ref(str(item))
        if ref:
            out.append(ref)
    return out


def ref_snapshot(ref: dict) -> dict:
    """任务里存的参考图快照 —— 字段要与 `reuse_image_refs` 的还原逻辑对齐"""
    return {
        "id": ref.get("id"),
        "fileName": ref.get("fileName") or "",
        "originalName": ref.get("originalName") or "",
        "localPath": ref.get("localPath") or "",
        "localUrl": ref.get("localUrl") or "",
        "mimeType": ref.get("mimeType") or "",
        "size": ref.get("size") or 0,
        "url": ref.get("url") or "",
    }


# ============================================================ 任务创建 / 重试 / 取消


def create_image_task(
    *,
    model_id: str | None,
    prompt: str,
    size: str = "",
    count: int = DEFAULT_COUNT,
    ref_ids: list | None = None,
) -> dict:
    model = find_image_model(model_id)
    if not model:
        raise ValueError("请选择图片模型")
    if not model.get("enabled"):
        raise ValueError(f"图片模型「{model.get('name')}」已停用")

    text = str(prompt or "").strip()
    if not text:
        raise ValueError("请填写提示词")

    refs = pick_refs(ref_ids)
    if ref_ids and not refs:
        raise ValueError("参考图已失效，请重新选择")
    if refs and not model_supports_image_input(model):
        raise ValueError(
            f"图片模型「{model.get('name')}」未开启图生图，请在模型管理里勾选「支持图生图」或换一个模型"
        )

    try:
        n = int(count)
    except (TypeError, ValueError):
        n = DEFAULT_COUNT
    n = max(1, min(MAX_COUNT, n))

    task = new_image_task(
        model_id=model["id"],
        model_name=model.get("name") or "",
        prompt=text,
        size=size or model.get("size") or "",
        count=n,
        refs=[ref_snapshot(r) for r in refs],
    )
    store.image_tasks.insert(0, task)
    store.mark_dirty("image_tasks")
    store.save()
    return task


def retry_image_task(task: dict) -> dict:
    if task.get("status") == "running":
        raise ValueError("任务正在生成中，无法重试")
    task.update(
        {
            "status": "queued",
            "error": "",
            "images": [],
            "progress": "",
            "cancelRequested": False,
            "startedAt": None,
            "finishedAt": None,
        }
    )
    store.mark_dirty("image_tasks")
    store.save()
    return task


def cancel_image_task(task: dict) -> dict:
    if task.get("status") == "running":
        raise ValueError("任务已在生成中，无法取消（请求已发出）")
    if task.get("status") != "queued":
        raise ValueError("任务已结束，无需取消")
    task["status"] = "cancelled"
    task["finishedAt"] = now_ms()
    store.mark_dirty("image_tasks")
    store.save()
    return task


# ============================================================ 执行


async def run_image_task(task: dict) -> None:
    model = find_image_model(task.get("modelId"))
    if not model:
        task["status"] = "failed"
        task["error"] = "图片模型已被删除"
        task["finishedAt"] = now_ms()
        store.mark_dirty("image_tasks")
        store.save()
        return

    task["status"] = "running"
    task["startedAt"] = now_ms()
    task["attempts"] = int(task.get("attempts") or 0) + 1
    task["error"] = ""
    store.mark_dirty("image_tasks")
    store.save()

    def on_progress(text: str) -> None:
        task["progress"] = text
        store.mark_dirty("image_tasks")

    images: list[dict] = []
    try:
        items = await imagegen.generate_images(
            model=model,
            prompt=str(task.get("prompt") or ""),
            size=str(task.get("size") or ""),
            count=int(task.get("count") or 1),
            refs=task.get("refs") or [],
            on_progress=on_progress,
        )
        for item in items:
            buffer, ext = await imagegen.download_image(item)
            name = f"{next_id('gen')}{ext}"
            dest = get_paths().generated / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(buffer)
            images.append(
                {
                    "id": next_id("gi"),
                    "fileName": name,
                    "localPath": str(dest),
                    "localUrl": f"/files/generated/{name}",
                    "remoteUrl": item.get("url") or "",
                    "size": len(buffer),
                    "usedBy": None,
                }
            )
        task["images"] = images
        task["status"] = "success"
        task["progress"] = ""
    except Exception as err:  # noqa: BLE001 - 失败要落到任务上，不能吞
        task["status"] = "failed"
        task["error"] = str(err)
        task["progress"] = ""
    finally:
        task["finishedAt"] = now_ms()
        store.mark_dirty("image_tasks")
        store.save()
        store.flush()


# ============================================================ 调度循环


class _State:
    ticking = False
    task: asyncio.Task | None = None


def _spawn(coro, *, label: str) -> asyncio.Task:
    t = asyncio.get_running_loop().create_task(coro)

    def _done(fut: asyncio.Task) -> None:
        if fut.cancelled():
            return
        err = fut.exception()
        if err is not None:
            log.warning("[image-jobs] %s 未捕获异常：%s", label, err)

    t.add_done_callback(_done)
    return t


async def tick() -> None:
    if _State.ticking:
        return
    _State.ticking = True
    try:
        queued = sorted(
            [t for t in store.image_tasks if t.get("status") == "queued"],
            key=lambda t: int(t.get("createdAt") or 0),
        )
        if not queued:
            return
        task = queued[0]
        # 生图是全局串行的：这里 await 会把整轮 tick 占住，
        # 正是原版 `working` 标志的效果，不需要额外加锁。
        await run_image_task(task)
    finally:
        _State.ticking = False


async def _tick_loop() -> None:
    while True:
        try:
            await tick()
        except Exception as err:  # noqa: BLE001
            log.warning("[image-jobs] tick 异常：%s", err)
        await asyncio.sleep(TICK_MS / 1000)


def start_image_worker() -> None:
    """启动生图队列。幂等。"""
    if _State.task is None or _State.task.done():
        _State.task = _spawn(_tick_loop(), label="image-tick-loop")


async def stop_image_worker() -> None:
    if _State.task is not None and not _State.task.done():
        _State.task.cancel()
        try:
            await _State.task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    _State.task = None


# ============================================================ 给路由层用的薄封装


def set_paths(paths: Paths) -> None:
    """与 scheduler 共用同一份存储位置（main.py 启动时调一次即可）"""
    from . import scheduler

    scheduler.set_paths(paths)
