"""Topaz Video AI 联动 —— 复刻 `server/topaz.js` 的**探测 / 快照 / 候选 / 配置**层。

原版 55.3KB，不直接调 `Topaz Video AI.exe`（那是个 GUI），而是用它自带的 `ffmpeg.exe` ——
它暴露了 `tvai_up`（超分/增强）等滤镜，能离线跑本地已有的模型，命令行可控、能出进度。

本机实测（RTX 3050 Laptop 4GB / i5-12500H）：1080p 2× 到 4K 约 3 fps，vram 压到 0.5 稳定。
所以档位都按 4GB 显存保守设计，**一次只跑一个任务**，避免显存打满崩掉。

## 本期范围（按四项决策）

- 已完成：引擎探测（在 `video_frames.py`）、档位 / 质感档定义、本地文件夹扫描与预览、
  任务记录快照、候选清单（待提升 / 已提升）。
- 留空壳：真正跑 `tvai_fi` + `tvai_up` 的增强流水线（`start_enhance` / `start_batch` /
  `cancel_enhance` / `restore`）。它们会启动 ffmpeg 子进程、跑几分钟到几十分钟，
  按决策本期不做，但**接口形状已经定好**，前端调用不会因为缺函数而崩。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from .store import store
from .video_frames import resolve_topaz, set_topaz_dir  # noqa: F401 - 对外统一从本模块导出

#: 认哪些扩展名算视频。生成结果、本地文件夹两条路共用同一份，免得两边标准不一致。
VIDEO_EXTS = [".mp4", ".mov", ".mkv", ".webm", ".avi", ".flv", ".m4v", ".wmv", ".mpeg", ".mpg"]

#: 本地文件夹一次最多列这么多条（新的排前面）。
#: 别调太大：每一条在弹窗里都是一个 `<video>` 缩略图，几百个会同时开一堆解码器。
MAX_LOCAL_FILES = 60
#: 子目录最多往下找这么深
LOCAL_WALK_DEPTH = 4

#: 识别「提升成品」的文件名：`xxx-topaz.mp4` / `xxx-topaz-1a2b3c.mp4`
TOPAZ_OUT_NAME_RE = "-topaz(?:-[0-9a-f]{6})?\\.mp4$"


def is_topaz_out_name(name: str) -> bool:
    """本地文件夹扫描时靠它把成品排除出「待提升」—— 成品被拷回本地文件夹这种事很常见，
    不挡一下的话用户一勾就会对着已经提升过的片子再跑一遍。
    """
    import re

    return bool(re.search(TOPAZ_OUT_NAME_RE, str(name or ""), re.I))


# ============================================================ 档位 / 质感档


TOPAZ_LOOKS = [
    {
        "id": "no-lift",
        "label": "不增亮（默认 · 压高光，防脸白）",
        "desc": "只压掉最亮的那一档（皮肤高光、额头反光），暗部和中调一点不动；再微降饱和 + 加极细颗粒，让人脸不再越提升越白、越光",
        # 拐点 160：<=160 的像素原样输出（暗部、普通肤色完全不动），
        # 超过 160 的部分按 0.8 收缩 —— 实测同一帧 p99 亮度 243→228、>235 的纯白像素 1.7%→0%，
        # 而中位亮度只从 153 掉到 152，整体不变暗。这就是「不加曝光」的精确做法。
        "filter": "lutyuv=y='if(gt(val,160),160+(val-160)*0.8,val)':u=val:v=val,"
        "hue=s=0.98,"
        "noise=alls=2:allf=t+u",
    },
    {
        "id": "natural",
        "label": "自然（只提升清晰，不改色调）",
        "desc": "提升后不做任何调色，亮暗、颜色和原片保持一致，只是变清晰了",
        "filter": "",
    },
    {
        "id": "soft",
        "label": "柔和（微降饱和 · 细颗粒）",
        "desc": "不改变亮暗，只降一点点饱和、加极细颗粒，减弱 AI 的「塑料光感」",
        "filter": "hue=s=0.96,noise=alls=2:allf=t+u",
    },
    {
        "id": "bright",
        "label": "明亮通透（提亮 · 更干净）",
        "desc": "适合本身就偏暗的素材：提亮中间调、略加饱和（暗素材慎用，会压得更亮）",
        "filter": "lutyuv=y='16+pow(clip((val-16)/219,0,1),0.96)*219':u=val:v=val," "hue=s=1.03",
    },
]

TOPAZ_TIERS = [
    {
        "id": "std",
        "label": "提升画质（Proteus 2× · 60fps）",
        "desc": "Chronos（标准版）补帧到 60fps + Proteus 放大 2×（1080p → 4K；恢复压缩 80 / 恢复细节 60 / 锐化 10），不开运动模糊脱模，最后 H265 / NVIDIA 出片。参数与 Topaz GUI 验证过的一套一致。",
    },
]


def find_tier(tier_id: str | None) -> dict:
    for tier in TOPAZ_TIERS:
        if tier["id"] == tier_id:
            return tier
    return TOPAZ_TIERS[-1]


def get_look() -> dict:
    """当前选的质感档；没选过 / 选的值已下线 → 默认「不增亮」"""
    wanted = str((store.topaz_local or {}).get("look") or "")
    for look in TOPAZ_LOOKS:
        if look["id"] == wanted:
            return look
    return TOPAZ_LOOKS[0]


def set_topaz_look(look_id: str) -> dict:
    """改质感档（存库，重启后还在）"""
    look = next((l for l in TOPAZ_LOOKS if l["id"] == look_id), None)
    if not look:
        raise ValueError("未知的质感档")
    cfg = dict(store.topaz_local or {})
    cfg["look"] = look["id"]
    store.replace("topaz_local", cfg)
    store.save()
    store.flush()
    return {"look": look["id"], "label": look["label"]}


# ============================================================ 本地文件夹


def _path_key(path: str) -> str:
    """路径比对键：Windows 大小写不敏感，统一小写；其它平台原样"""
    resolved = str(Path(path).resolve())
    return resolved.lower() if os.name == "nt" else resolved


def get_local_dir() -> str:
    """用户指定的本地文件夹（存在库里，重启后还在）"""
    return str((store.topaz_local or {}).get("dir") or "").strip()


def set_local_dir(directory) -> dict:
    """手动指定 / 清空本地文件夹"""
    cfg = dict(store.topaz_local or {})
    cfg["dir"] = str(directory or "").strip()
    store.replace("topaz_local", cfg)
    store.save()
    store.flush()
    return scan_local_dir()


def is_video_path(path) -> bool:
    return Path(str(path or "")).suffix.lower() in VIDEO_EXTS


def is_local_video_file(path) -> bool:
    """这个本地文件确实存在、且是视频（预览路由和候选扫描都靠它把关）"""
    raw = str(path or "").strip()
    if not raw or not os.path.isabs(raw) or not is_video_path(raw):
        return False
    try:
        return os.path.isfile(raw)
    except OSError:
        return False


def local_preview_url(path: str) -> str:
    """本地视频给前端的预览地址（带 Range 流式返回，`<video>` 才能拖动/取首帧）"""
    from urllib.parse import quote

    return f"/api/topaz/local-file?path={quote(str(path), safe='')}"


def scan_local_dir(directory: str | None = None) -> dict:
    """扫一个文件夹里的视频（含子目录，最多 4 层 / 60 条），按修改时间新的排前面。

    每条带上预览地址，前端拿它当缩略图（和生成结果那边的 `<video src>` 一个用法）。
    """
    root = str(directory if directory is not None else get_local_dir()).strip()
    out: dict = {"dir": root, "items": [], "error": ""}
    if not root:
        return out
    if not os.path.isdir(root):
        out["error"] = "这个目录不存在，检查一下路径"
        return out

    found: list[dict] = []

    def walk(current: str, depth: int) -> None:
        if len(found) >= MAX_LOCAL_FILES or depth > LOCAL_WALK_DEPTH:
            return
        try:
            entries = list(os.scandir(current))
        except OSError:
            return  # 没权限的子目录直接跳过，别整个扫描失败
        for entry in entries:
            if len(found) >= MAX_LOCAL_FILES:
                return
            path = os.path.join(current, entry.name)
            try:
                if entry.is_dir():
                    walk(path, depth + 1)
                    continue
            except OSError:
                continue
            if not is_video_path(entry.name):
                continue
            try:
                stat = entry.stat()
            except OSError:
                continue
            found.append({"path": path, "name": entry.name, "size": stat.st_size, "mtime": stat.st_mtime * 1000})

    walk(root, 0)
    found.sort(key=lambda f: -f["mtime"])
    out["items"] = [{**f, "url": local_preview_url(f["path"])} for f in found]
    return out


# ============================================================ 任务记录


def _public_job(job: dict) -> dict:
    """给前端的快照：不塞进程对象等不能序列化的东西"""
    return {
        "id": job.get("id"),
        # source 必须带上：恢复时要读 `source || 'task'`，漏了的话重启后
        # video / local 来源的任务全被当成 task，去重和按来源取消就都找不到了
        "source": job.get("source"),
        "taskId": job.get("taskId"),
        "index": job.get("index"),
        "tier": job.get("tier"),
        "tierLabel": job.get("tierLabel"),
        "fileName": job.get("fileName"),
        "status": job.get("status"),
        "progress": max(0.0, min(1.0, float(job.get("progress") or 0))),
        "speed": job.get("speed"),
        "message": job.get("message"),
        "error": job.get("error"),
        "startedAt": job.get("startedAt"),
        "finishedAt": job.get("finishedAt"),
        "outUrl": job.get("outUrl"),
        "sourceUrl": job.get("sourceUrl"),
    }


def topaz_jobs_snapshot() -> list[dict]:
    return [_public_job(j) for j in (store.topaz_jobs or [])[:30]]


def clear_topaz_failed_jobs() -> dict:
    """清空「没成功的」记录（失败 / 已取消）。

    这些记录只在弹窗里当「上次为什么没成」的提示用，攒多了只会一直挂在那儿碍眼。
    只清终态失败 / 取消的：排队中、正在跑的不动；成功的那条已经写进结果本身
    （画布 / 视频上的 topaz 标记），跟这份记录没关系，清掉也不会丢成品。
    """
    rows = store.topaz_jobs
    before = len(rows)
    kept = [j for j in rows if j.get("status") not in ("failed", "canceled")]
    store.replace("topaz_jobs", kept)
    store.save()
    return {"removed": before - len(kept)}


def topaz_snapshot() -> dict:
    probe = resolve_topaz()
    return {
        "found": probe.get("found"),
        # 引擎是随包内置的，还是回落到本机另外装的 Topaz
        "bundled": bool(probe.get("bundled")),
        "ffmpeg": probe.get("ffmpeg"),
        "dir": probe.get("dir"),
        "modelsReady": probe.get("modelsReady"),
        "tiers": [{"id": t["id"], "label": t["label"], "desc": t["desc"]} for t in TOPAZ_TIERS],
        # 质感档：默认「不增亮」（压皮肤高光），前端按这个渲染选择行
        "look": get_look()["id"],
        "looks": [{"id": l["id"], "label": l["label"], "desc": l["desc"]} for l in TOPAZ_LOOKS],
        # 本地文件夹：弹窗里那个输入框靠它回显；一次最多列多少条也给前端，好在界面上说明
        "localDir": get_local_dir(),
        "localLimit": MAX_LOCAL_FILES,
        "jobs": topaz_jobs_snapshot(),
    }


# ============================================================ 候选清单


def _local_result_for(path: str) -> dict | None:
    """查这个本地文件提升过没有（本地没有 task 可回写，只能自己记一份）"""
    for rec in (store.topaz_local or {}).get("results") or []:
        if rec.get("sourcePath") == path:
            return rec
    return None


def _adopt_local_result(item: dict) -> dict | None:
    """原片被挪过位置时的补救：提升记录里存的是老路径，文件挪走后老路径已经失效，
    按路径匹配就永远对不上，新位置会被当成「没提升过」。

    这里按文件名过继：老记录的原片确实已经不在磁盘上 + 文件名完全一致 才认，
    名字还在的老文件一律不动（宁可不认也不能把没提升过的当成提升过）。
    """
    name = Path(item["path"]).name.lower()
    for rec in (store.topaz_local or {}).get("results") or []:
        source = rec.get("sourcePath")
        if not source:
            continue
        if _path_key(source) == _path_key(item["path"]):
            continue
        if os.path.exists(source):
            continue
        if Path(source).name.lower() == name:
            return rec
    return None


def _is_video_out(out: dict | None) -> bool:
    """一条结果是不是「视频」，且文件还在本地"""
    if not out or not out.get("localPath"):
        return False
    file_type = str(out.get("fileType") or "").lower()
    is_video = file_type in ("mp4", "video") or is_video_path(out["localPath"])
    if not is_video:
        return False
    return os.path.exists(out["localPath"])


def topaz_candidates() -> dict:
    pending: list[dict] = []
    done: list[dict] = []
    # 画布 / 视频生成记录里出现过的成品路径：本地文件夹扫描时跳过这些路径，
    # 不然用户把成品直接存进（或下载进）自己扫的那个文件夹时，同一条片子会
    # 以「自用」和「本地」两个身份各出现一次，看起来就是列表里有重复。
    claimed: set[str] = set()

    def push(base: dict, out: dict | None) -> None:
        # 认领路径必须放在 _is_video_out 之前：成品文件被挪走 / 清理后 _is_video_out 会判 False，
        # 但「这条已经提升过」的事实还在库里 —— 原片路径照样要认领，
        # 不然本地文件夹扫描会把原片重新列成「待提升」（已提升的文件阴魂不散）。
        if out and out.get("localPath"):
            claimed.add(_path_key(out["localPath"]))
        # 已提升的结果还要把「原片路径」也认领掉：原片还躺在用户的本地文件夹里，
        # 不认领的话本地扫描会把它当「待提升」再列一次 —— 同一条片子既在已提升又在待提升。
        if out and (out.get("topaz") or {}).get("sourcePath"):
            claimed.add(_path_key(out["topaz"]["sourcePath"]))
        if not _is_video_out(out):
            return
        topaz = (out or {}).get("topaz") or {}
        if topaz.get("at"):
            done.append(
                {
                    **base,
                    "tier": topaz.get("tier"),
                    "tierLabel": topaz.get("tierLabel"),
                    "outWidth": topaz.get("outWidth"),
                    "outHeight": topaz.get("outHeight"),
                    "at": topaz.get("at"),
                }
            )
        else:
            pending.append(base)

    # ① 自用模块（RunningHub 画布）的多结果
    tasks = sorted(store.tasks, key=lambda t: -(int(t.get("createdAt") or 0)))
    for task in tasks:
        outputs = task.get("outputs") if isinstance(task.get("outputs"), list) else []
        for index, out in enumerate(outputs):
            if not out:
                continue
            push(
                {
                    "source": "task",
                    "taskId": task.get("id"),
                    "index": index,
                    "canvasId": task.get("canvasId") or "",
                    "canvasName": task.get("canvasName") or "（未知画布）",
                    "accountName": task.get("accountName") or "",
                    "createdAt": task.get("createdAt") or 0,
                    "fileName": Path(out["localPath"]).name if out.get("localPath") else "",
                    "url": out.get("localUrl") or "",
                    "size": out.get("size") or 0,
                },
                out,
            )

    # ② 视频生成模块（图生视频）的成品 —— 一条任务一个视频
    for task in sorted(store.video_tasks, key=lambda t: -(int(t.get("createdAt") or 0))):
        out = task.get("video")
        push(
            {
                "source": "video",
                "taskId": task.get("id"),
                "index": 0,
                "canvasId": "",
                "canvasName": task.get("modelName") or "（视频生成）",
                "accountName": "",
                "createdAt": task.get("finishedAt") or task.get("createdAt") or 0,
                "fileName": Path(out["localPath"]).name if out and out.get("localPath") else "",
                "url": (out or {}).get("localUrl") or "",
                "size": (out or {}).get("size") or 0,
                # 给界面显示：让用户知道这条是哪来的
                "originLabel": "视频生成",
                "desc": str(task.get("prompt") or "")[:60],
            },
            out,
        )

    # ③ 用户指定的本地文件夹里的视频（弹窗里贴路径「读取」出来的）
    local_dir = get_local_dir()
    if local_dir:
        for item in scan_local_dir(local_dir)["items"]:
            # 提升成品（文件名带 -topaz）不进候选：它已经是结果了，列进「待提升」只会被二次提升。
            # 用户要看成品，去「画质提升结果」目录看（默认在输出目录的 topaz 文件夹）。
            if is_topaz_out_name(item["name"]):
                continue
            # 这条文件已经被画布 / 视频生成的任务记录占了（成品就存在这个文件夹里），
            # 上面 ①② 已经按任务身份列过一次，这里再列就是重复 —— 跳过，保留任务那条
            # （任务条目带画布名 / 账号信息，还能还原原片，比光秃秃的本地条目有用）。
            if _path_key(item["path"]) in claimed:
                continue
            base = {
                "source": "local",
                "taskId": "",
                "index": 0,
                "canvasId": "",
                "canvasName": "本地文件",
                "accountName": "",
                "createdAt": item["mtime"],
                "fileName": item["name"],
                "url": item["url"],  # 预览流地址，前端当缩略图用
                "size": item["size"],
                "localPath": item["path"],
                "originLabel": "本地",
            }
            rec = _local_result_for(item["path"]) or _adopt_local_result(item)
            if rec:
                done.append(
                    {
                        **base,
                        "tier": rec.get("tier"),
                        "tierLabel": rec.get("tierLabel"),
                        "outWidth": rec.get("outWidth"),
                        "outHeight": rec.get("outHeight"),
                        "at": rec.get("at"),
                    }
                )
            else:
                pending.append(base)

    return {"pending": pending[:200], "done": done[:100]}


def norm_source(source) -> str:
    """来源归一化：只认这仨，别的都当自用模块的 task"""
    return "video" if source == "video" else "local" if source == "local" else "task"


# ============================================================ 增强流水线（本期空壳）


class TopazNotImplemented(NotImplementedError):
    """按四项决策，真正的增强流水线本期不做。"""


_NOT_READY = (
    "提升画质的执行流水线本期未实现（按「骨架 + 核心链路」的范围决策，Topaz 只保留"
    "引擎探测、档位配置与候选清单）。需要时补齐 tvai_fi + tvai_up + hevc_nvenc 那一套。"
)


def start_enhance(*, source, task_id=None, index=0, local_path=None, tier=None) -> dict:
    """用本机增强引擎提升某个视频结果的画质（空壳）。"""
    raise TopazNotImplemented(_NOT_READY)


def start_batch(*, items, tier=None) -> dict:
    """批量排队提升：一次提交勾选的多条（档位统一）（空壳）。"""
    if not (items if isinstance(items, (list, tuple)) else []):
        raise ValueError("没有勾选任何视频")
    raise TopazNotImplemented(_NOT_READY)


def cancel_enhance(*, job_id=None, source=None, task_id=None, index=0) -> dict:
    """取消一条提升任务（空壳）。"""
    raise TopazNotImplemented(_NOT_READY)


def restore(*, source, task_id=None, index=0) -> dict:
    """还原原片：把结果换回 Topaz 处理前的文件（空壳）。"""
    raise TopazNotImplemented(_NOT_READY)
