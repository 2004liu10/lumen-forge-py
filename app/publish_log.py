"""发布记录 —— 复刻 `server/publish-log.js`（10376 字节）。

## 解决「发完不记得，下一条又发成上一条」

用户的原始问题：每次发完视频，下次再发就容易把同一个视频又发一遍。
根因是发布页的「已选视频」只是一个表单字段 —— 发完不清理、也没留任何记录，
下次回到这个模块，表单里还是那条视频，顺手一点「自动上传并填写」就重发了。

这里记两件事：

1. 谁被「填过表」（status=filled）—— 视频已经进了抖音的上传框，等着用户点发布
2. 谁被「确认发过」（status=published）—— 用户点了发布 / 或者跳到了作品管理页

**身份判定用 path + size + mtime 三者一起**，而不是只看路径：
同一个路径的视频如果被重新导出过（剪辑软件再出一版），mtime 和 size 会变，
那就该当成一条新作品，不能因为「路径见过」就拦着不让发。
"""

from __future__ import annotations

import os
from pathlib import Path

from .ids import next_id, now_ms
from .shared_files import count_references
from .store import store

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi"}


def file_identity(file_path) -> dict | None:
    """视频文件的当前身份（文件不在了就返回 None，调用方据此提示「文件没了」）"""
    raw = str(file_path or "").strip()
    if not raw:
        return None
    try:
        stat = os.stat(raw)
    except OSError:
        return None
    if not os.path.isfile(raw):
        return None
    return {"path": str(Path(raw).resolve()), "size": stat.st_size, "mtimeMs": round(stat.st_mtime * 1000)}


def _same_file(a: dict | None, b: dict | None) -> bool:
    return bool(
        a
        and b
        and a.get("path") == b.get("path")
        and a.get("size") == b.get("size")
        and a.get("mtimeMs") == b.get("mtimeMs")
    )


def find_record_by_identity(identity: dict | None) -> dict | None:
    """找这条视频的记录（同 path + size + mtime）。没有就返回 None"""
    if not identity:
        return None
    for rec in store.publish_log:
        if _same_file(rec, identity):
            return rec
    return None


def find_latest_by_path(file_path) -> dict | None:
    """找「这条路径下最近的一条记录」，不管内容变没变。
    用来提示「这个文件之前发过，但内容好像改过了」。
    """
    raw = str(file_path or "").strip()
    if not raw:
        return None
    try:
        resolved = str(Path(raw).resolve())
    except OSError:
        resolved = raw
    rows = [r for r in store.publish_log if r.get("path") == resolved]
    if not rows:
        return None

    def when(rec: dict) -> int:
        return int(rec.get("publishedAt") or rec.get("filledAt") or rec.get("createdAt") or 0)

    return sorted(rows, key=when, reverse=True)[0]


def mark_filled(*, path, name=None, desc: str = "", topics=None) -> dict:
    """登记一次「已填表」。同一条视频（同身份）重复填时更新原记录，不新增。

    返回记录本身。
    """
    identity = file_identity(path)
    if not identity:
        raise ValueError("找不到这个视频文件，可能被移动或删除了")

    now = now_ms()
    rec = find_record_by_identity(identity)
    if rec:
        # 又填了一次：状态回到 filled（上一轮可能只是填了没发），并刷新文案
        rec["status"] = "published" if rec.get("status") == "published" else "filled"
        rec["desc"] = str(desc or "")
        rec["topics"] = list(topics)[:20] if isinstance(topics, (list, tuple)) else []
        rec["filledAt"] = now
        rec["fillCount"] = int(rec.get("fillCount") or 1) + 1
    else:
        rec = {
            "id": next_id("pub"),
            "path": identity["path"],
            "size": identity["size"],
            "mtimeMs": identity["mtimeMs"],
            "name": str(name or Path(identity["path"]).name),
            "desc": str(desc or ""),
            "topics": list(topics)[:20] if isinstance(topics, (list, tuple)) else [],
            "status": "filled",
            "publishedAt": 0,
            "filledAt": now,
            "fillCount": 1,
            "createdAt": now,
        }
        store.publish_log.append(rec)
    store.mark_dirty("publish_log")
    store.save()
    store.flush()
    return rec


def mark_published(record_id: str) -> dict:
    """确认发布完成（用户点「标记已发布」，或页面跳到了作品管理页）"""
    rec = store.find("publish_log", id=record_id)
    if not rec:
        raise ValueError("这条发布记录不存在")
    rec["status"] = "published"
    rec["publishedAt"] = now_ms()
    store.mark_dirty("publish_log")
    store.save()
    store.flush()
    return rec


def mark_published_by_identity(file_path) -> dict | None:
    """按身份直接标记发布（自动检测那条路用：只知道视频路径，不一定有 id）"""
    identity = file_identity(file_path)
    rec = find_record_by_identity(identity)
    if not rec:
        return None
    return mark_published(rec["id"])


def unmark_published(record_id: str) -> dict:
    """撤销「已发布」（误点了，或者其实没发成功）"""
    rec = store.find("publish_log", id=record_id)
    if not rec:
        raise ValueError("这条发布记录不存在")
    rec["status"] = "filled"
    rec["publishedAt"] = 0
    store.mark_dirty("publish_log")
    store.save()
    store.flush()
    return rec


def remove_record(record_id: str) -> None:
    rows = store.publish_log
    for index, rec in enumerate(rows):
        if rec.get("id") == record_id:
            del rows[index]
            store.mark_dirty("publish_log")
            store.save()
            store.flush()
            return
    raise ValueError("这条发布记录不存在")


def clear_records() -> int:
    count = len(store.publish_log)
    store.replace("publish_log", [])
    store.save()
    store.flush()
    return count


def list_publish_works() -> dict:
    """列出「可以直接拿去发布的作品」—— 程序里自己生成的视频。

    解决什么：发布页原来只能弹系统文件框手动翻文件夹。而自己生成的视频明明就在
    程序里（视频生成模块的成品、自用模块的画布结果），却选不到，得先下载再去找。

    每条带上两个状态，省得用户点开才发现：

    - `enhanced` —— 是否已经用 Topaz 提升过画质
    - `published` —— 是否已经发布过（按 path+size+mtime 跟发布记录对）
    """
    works: list[dict] = []

    # ① 视频生成模块（图生视频）的成品
    for task in store.video_tasks:
        out = task.get("video")
        if not out or not out.get("localPath"):
            continue
        works.append(
            {
                "source": "video",
                "taskId": task.get("id"),
                "index": 0,
                "path": out["localPath"],
                "name": out.get("fileName") or Path(out["localPath"]).name,
                "size": out.get("size") or 0,
                "url": out.get("localUrl") or "",
                "origin": "视频生成",
                "desc": str(task.get("prompt") or "")[:70],
                "enhanced": bool((out.get("topaz") or {}).get("at")),
                "enhancedLabel": (out.get("topaz") or {}).get("tierLabel") or "",
                "createdAt": task.get("finishedAt") or task.get("createdAt") or 0,
            }
        )

    # ② 自用模块（RunningHub 画布）的视频结果
    for task in store.tasks:
        outputs = task.get("outputs") if isinstance(task.get("outputs"), list) else []
        for index, out in enumerate(outputs):
            if not out or not out.get("localPath"):
                continue
            ext = Path(out["localPath"]).suffix.lower()
            if ext not in VIDEO_EXTS:
                continue
            works.append(
                {
                    "source": "task",
                    "taskId": task.get("id"),
                    "index": index,
                    "path": out["localPath"],
                    "name": Path(out["localPath"]).name,
                    "size": out.get("size") or 0,
                    "url": out.get("localUrl") or "",
                    "origin": task.get("canvasName") or "自用",
                    "desc": "",
                    "enhanced": bool((out.get("topaz") or {}).get("at")),
                    "enhancedLabel": (out.get("topaz") or {}).get("tierLabel") or "",
                    "createdAt": task.get("createdAt") or 0,
                }
            )

    # 只留文件真实还在的。任务记录会一直留着，但视频文件可能早就被删掉了 ——
    # 那种「记录还在、片子没了」的条目挑也挑不动，不该占列表。
    alive: list[dict] = []
    hidden = 0
    for work in works:
        try:
            exists = os.path.isfile(work["path"])
        except OSError:
            exists = False
        if not exists:
            hidden += 1
            continue
        # 标出哪些已经发布过（用文件身份对，不是只对路径 —— 重新导出过的算新作品）
        hit = find_record_by_identity(file_identity(work["path"])) or find_latest_by_path(work["path"])
        work["published"] = bool(hit and hit.get("status") == "published")
        work["publishedAt"] = (hit or {}).get("publishedAt") or 0
        alive.append(work)

    alive.sort(key=lambda w: -(int(w.get("createdAt") or 0)))
    # hidden = 被略过的失效条数，让界面能说明「少掉的那些是文件已经没了」
    return {"works": alive[:200], "hidden": hidden}


def analyze_deletable(*, include_filled: bool = False) -> dict:
    """分析「发布过的视频」里哪些能删、哪些不能删。

    这是破坏性操作的前置检查，判断必须保守：

    - 文件不存在 → 不用删（列出来让用户知道记录指向的文件已经没了）
    - 还被别处引用（画布素材 / 视频模块的输入图或结果）→ **不能删**，
      删了那边的记录就废了。列出来并说明被谁用着
    - 都没问题 → 可以删

    `include_filled`：是否把「填过表但没确认发布」的也算进来。
    默认不算 —— 那种状态没法确定用户到底发没发出去，删了可能误伤。
    """
    rows = [
        r
        for r in store.publish_log
        if r.get("status") == "published" or (include_filled and r.get("status") == "filled")
    ]
    items: list[dict] = []
    for rec in rows:
        try:
            stat = os.stat(rec.get("path") or "")
            exists = os.path.isfile(rec.get("path") or "")
            size = stat.st_size if exists else 0
        except OSError:
            exists = False
            size = 0
        # count_references 默认把「发布记录」也算作占用（成品不该被顺手清掉）。
        # 但这里正是那个「显式清理」的入口，要是把自己也算进去，所有文件都会
        # 被判成「有人用」，永远删不掉 —— 所以要把发布记录排除掉，
        # 只数画布素材 / 任务结果 / 参考图 / 视频输入图这些**真正的使用方**。
        refs = count_references(rec.get("path"), include_publish_log=False) if exists else 0
        items.append(
            {
                "id": rec.get("id"),
                "path": rec.get("path"),
                "name": rec.get("name") or Path(rec.get("path") or "").name,
                "size": size,
                "exists": exists,
                "status": rec.get("status"),
                "publishedAt": rec.get("publishedAt") or 0,
                "filledAt": rec.get("filledAt") or 0,
                "refs": refs,
                "usedElsewhere": refs > 0,
            }
        )

    deletable = [i for i in items if i["exists"] and not i["usedElsewhere"]]
    in_use = [i for i in items if i["exists"] and i["usedElsewhere"]]
    missing = [i for i in items if not i["exists"]]

    return {
        "items": items,
        "deletable": deletable,
        "inUse": in_use,
        "missing": missing,
        "totalCount": len(items),
        "deletableCount": len(deletable),
        "deletableBytes": sum(int(i.get("size") or 0) for i in deletable),
        "inUseCount": len(in_use),
        "missingCount": len(missing),
        "allBytes": sum(int(i.get("size") or 0) for i in items),
    }


def mark_deleted(ids=None) -> int:
    """记下「这些视频的本地文件已经删了」。

    刻意**保留记录本身**：以后还要靠它认出「这条发过了」，
    不能因为文件删了就把它忘掉（忘了的话，万一文件又从别处回来就会重发）。
    只加一个 `deletedAt` 时间戳。
    """
    wanted = {str(x) for x in (ids if isinstance(ids, (list, tuple)) else [])}
    now = now_ms()
    count = 0
    for rec in store.publish_log:
        if rec.get("id") not in wanted:
            continue
        rec["deletedAt"] = now
        count += 1
    if count:
        store.mark_dirty("publish_log")
        store.save()
        store.flush()
    return count


def list_publish_records() -> list[dict]:
    """列表：新的排前面。顺手补一个「文件还在吗」—— 文件被删了要显出来，别让用户白找"""
    rows = list(store.publish_log)

    def when(rec: dict) -> int:
        return int(rec.get("publishedAt") or rec.get("filledAt") or rec.get("createdAt") or 0)

    rows.sort(key=when, reverse=True)
    return [{**r, "fileExists": os.path.exists(r.get("path") or "")} for r in rows]


def check_publish(file_path) -> dict:
    """查一条视频能不能再发：给发布页在「选中视频」「点上传」两处用。

    返回 `{blocked, warn, record, reason}`：

    - `blocked=True` → 已经发过，界面上应当拦一下（用户坚持的话仍可继续）
    - `warn` → 只是填过表还没确认发过，提示一句
    """
    identity = file_identity(file_path)
    if not identity:
        # 文件不在（被移走 / 已经清理掉了）。这时**不能**直接说「查不到就算了」——
        # 那等于把「这条发过」这件事忘掉，万一用户从回收站还原回来就会重发。
        # 所以退回按路径查一次，把结论如实报出来。
        by_path = find_latest_by_path(file_path)
        if by_path and by_path.get("status") == "published":
            return {
                "blocked": True,
                "warn": False,
                "record": by_path,
                "reason": "这条视频已经发布过了（本地文件已被移走或清理）",
            }
        if by_path and by_path.get("status") == "filled":
            return {
                "blocked": False,
                "warn": True,
                "record": by_path,
                "reason": "这条视频填过表还没确认发布，但本地文件已经不在了",
            }
        return {"blocked": False, "warn": False, "record": None, "reason": "文件不存在"}

    exact = find_record_by_identity(identity)
    if exact and exact.get("status") == "published":
        return {"blocked": True, "warn": False, "record": exact, "reason": "这条视频已经发布过了"}
    if exact and exact.get("status") == "filled":
        return {
            "blocked": False,
            "warn": True,
            "record": exact,
            "reason": "这条视频刚填过表，可能已经在抖音页面上传好了",
        }
    # 同路径但内容变了：提一句，不当成重复
    by_path = find_latest_by_path(identity["path"])
    if by_path:
        return {
            "blocked": False,
            "warn": True,
            "record": by_path,
            "reason": "这个文件之前发过，但内容好像改过了",
        }
    return {"blocked": False, "warn": False, "record": None, "reason": ""}
