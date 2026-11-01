"""节点画布 + 本地素材库 —— 逐函数复刻 `server/node-canvas.js`（763 行 / 31.5KB）。

与 `server/index.js` 里的「画布」（RunningHub 应用卡片）不是一回事：
- 那边的 `canvases` 是应用参数表单；
- 这边的 `nodeCanvases` 是一张自由画布：节点 + 连线 + 视图位置，整份图原样存库。

节点画布里的 AI 生图 / 视频生成不另起一套任务队列，直接复用现有的
`image_jobs` / `video_jobs`，所以账号、模型、Key 都只有一份。

## 两条踩过坑的契约（移植时必须保住）

1. **`viewport` 的键名是 `x / y / zoom`**，快照原样下发（不是 `view`）。
2. **`nodeCanvasSnapshot()` 返回数组**，`nodeAssetSnapshot()` 也返回数组；
   前端 `nodecanvas.js` 自己按 id 建索引。早期骨架曾误写成字典，会直接让前端崩。
"""

from __future__ import annotations

import math
import os
import re
from pathlib import Path

from .ids import next_id
from .config import DATA_DIR
from .paths import Paths
from .shared_files import unlink_if_unused
from .store import store

#: 一张图最多这么多节点 / 连线：防止误导入一个巨型 JSON 把界面拖死
MAX_NODES = 400
MAX_EDGES = 800

#: 节点类型白名单：前端只认这几种，导入外部 JSON 时也必须过这一关
NODE_TYPES = {"text", "image", "video", "audio", "imageGen", "videoGen"}

#: AI 编导铺出来的节点带一份角色标记（定妆照 / 场景板 / 镜头图 / 镜头视频），
#: 「一键生成」按它分波次。scene = 场景板（同一场戏的环境参考图）。
AGENT_ROLES = {"cast", "scene", "shot", "video"}

#: 连线可选色（和 public/nodecanvas.css 里 .nc-edge[data-color="…"] 一一对应，别单边加）
EDGE_COLORS = {"teal", "violet", "gold", "rose", "slate"}

#: 视频节点入边的角色（和前端 edgeRoleFor / setEdgeRole 的取值一一对应）
EDGE_ROLES = {"start", "end", "ref"}

KIND_EXTS = {
    "image": [".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".avif"],
    "video": [".mp4", ".webm", ".mov", ".mkv", ".m4v", ".ogv"],
    "audio": [".mp3", ".wav", ".ogg", ".m4a", ".aac", ".flac"],
}

KIND_MIME = {
    "image": "image/png",
    "video": "video/mp4",
    "audio": "audio/mpeg",
}

#: 聊天记录上限
MAX_CHAT_MESSAGES = 120
MAX_CHAT_TEXT = 8000
MAX_CHAT_PHOTOS = 4
#: 一张画布的对话总量上限：别让一段闲聊把库撑爆
MAX_CHAT_TOTAL = 60000
#: 一张画布最多留几段历史对话
MAX_CHATS = 8
MAX_CHAT_TITLE = 32

#: `/files/<前缀>/...` 虚拟路径 → 真实目录的映射顺序（**refs 必须排在 generated 前面**）
FILES_PREFIX_KEYS = [
    ("generated/refs", "refs"),
    ("generated", "generated"),
    ("uploads", "uploads"),
    ("outputs", "outputs"),
    ("videos", "videos"),
    ("video-inputs", "video_inputs"),
    ("materials", "materials"),
]

_MEDIA_NAME_RE = re.compile(r"[^\w\-.]+")


# ============================================================ 小工具


def _text(value, max_len: int = 4000) -> str:
    """`text(value, max)` —— 转字符串并截断"""
    return str("" if value is None else value)[:max_len]


def _num(value, fallback, lo, hi):
    """`num(value, fallback, min, max)` —— 非有限数回落，其余夹在区间内"""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return fallback
    if not math.isfinite(parsed):
        return fallback
    return min(hi, max(lo, parsed))


def ext_kind(ext: str) -> str:
    """按扩展名（含点、小写）判断媒体大类，认不出返回空串"""
    for kind, exts in KIND_EXTS.items():
        if ext in exts:
            return kind
    return ""


def files_prefix_dirs(paths: Paths) -> list[tuple[str, Path]]:
    return [(prefix, getattr(paths, attr)) for prefix, attr in FILES_PREFIX_KEYS]


def path_from_files_url(url: str, paths: Paths) -> Path | None:
    """把 `/files/...` 反推成真实绝对路径；认不出前缀时按数据目录兜底"""
    raw = str(url or "")
    if not raw.startswith("/files/"):
        return None
    rel = raw[len("/files/"):].replace("%2F", "/").replace("%2f", "/")
    parts = rel.split("/")
    for prefix, directory in files_prefix_dirs(paths):
        prefix_parts = prefix.split("/")
        if parts[: len(prefix_parts)] == prefix_parts:
            tail = parts[len(prefix_parts):]
            return Path(directory, *tail)
    return Path(DATA_DIR, *parts)


def files_url_for(abs_path: str | Path, paths: Paths) -> str:
    """`pathFromFilesUrl` 的反函数：真实路径 → 可访问的 `/files/...` URL；不在已知目录里返回 ''"""
    target = Path(os.path.abspath(str(abs_path)))
    for prefix, directory in files_prefix_dirs(paths):
        rel = _relative_inside(Path(directory), target)
        if rel:
            return "/files/" + prefix + "/" + "/".join(_quote(p) for p in rel.parts)
    rel = _relative_inside(Path(DATA_DIR), target)
    if rel:
        return "/files/" + "/".join(_quote(p) for p in rel.parts)
    return ""


def _relative_inside(root: Path, target: Path) -> Path | None:
    try:
        rel = target.relative_to(Path(os.path.abspath(str(root))))
    except ValueError:
        return None
    return rel if str(rel) else None


def _quote(part: str) -> str:
    from urllib.parse import quote

    return quote(str(part), safe="")


def is_allowed_register_path(abs_path: str | Path, paths: Paths) -> bool:
    """允许登记的范围：七个媒体目录 + 数据目录本身"""
    target = Path(os.path.abspath(str(abs_path)))
    roots = [Path(os.path.abspath(str(d))).as_posix() for _, d in files_prefix_dirs(paths)]
    roots.append(Path(os.path.abspath(str(DATA_DIR))).as_posix())
    target_posix = target.as_posix()
    for root in roots:
        if target_posix == root or target_posix.startswith(root + "/"):
            return True
    return False


# ============================================================ 清洗导入数据


def sanitize_gen(raw) -> dict:
    """`gen` 字段白名单 —— 少存一个字段，重开画布就丢一项能力"""
    if not isinstance(raw, dict):
        return {}
    return {
        "modelId": _text(raw.get("modelId"), 80),
        "prompt": _text(raw.get("prompt"), 4000),
        # 重新生成时填的「这次想改什么」：和 prompt 分开存，
        # 这样改要求不会把提示词越堆越长，清空输入框就等于把这条要求删掉
        "revise": _text(raw.get("revise"), 500),
        "size": _text(raw.get("size"), 40),
        "count": round(_num(raw.get("count"), 1, 1, 4)),
        "status": _text(raw.get("status"), 20),
        # 分步进度文案（「准备参考图（第 1/4 步）」）：本地豆包桥那种长任务全靠它汇报进度
        "progress": _text(raw.get("progress"), 200),
        "taskId": _text(raw.get("taskId"), 80),
        "resultUrl": _text(raw.get("resultUrl"), 2000),
        "error": _text(raw.get("error"), 500),
        "duration": _text(raw.get("duration"), 20),
        "resolution": _text(raw.get("resolution"), 40),
        "ratio": _text(raw.get("ratio"), 20),
        "seed": _text(raw.get("seed"), 40),
        # 豆包内部模型档（2.5 / 2.0 / fast / mini）：不保留就会「每存一次盘被打回跟随当前档」
        "doubaoTier": _text(raw.get("doubaoTier"), 20),
    }


def sanitize_agent(raw) -> dict | None:
    """AI 编导留在节点上的标记。

    不带这个字段，画布一保存再打开就分不清「哪些是定妆照、哪些是镜头图」——
    「一键生成」要按 定妆照 → 镜头图 → 镜头视频 的顺序分波次跑。
    """
    if not isinstance(raw, dict):
        return None
    role = str(raw.get("role") or "")
    if role not in AGENT_ROLES:
        return None
    return {
        "role": role,
        "planId": _text(raw.get("planId"), 40),
        "planTitle": _text(raw.get("planTitle"), 120),
        "shotIndex": round(_num(raw.get("shotIndex"), 0, 0, 999)),
        "character": _text(raw.get("character"), 40),
        "motion": _text(raw.get("motion"), 1000),
        "dialogue": _text(raw.get("dialogue"), 200),
        # 「对话式改分镜」要用的原料：镜头原话、画风前缀、角色描述段、角色形象描述
        "scene": _text(raw.get("scene"), 600),
        "stylePrompt": _text(raw.get("stylePrompt"), 300),
        "castDesc": _text(raw.get("castDesc"), 600),
        # 景别 / 机位 / 光线：单拎出来存，改画面重拼提示词时还得放回原来的位置
        "shotSize": _text(raw.get("shotSize"), 12),
        "angle": _text(raw.get("angle"), 12),
        "light": _text(raw.get("light"), 60),
        "desc": _text(raw.get("desc"), 400),
        # 画幅的构图提示（「竖版构图，主体居中」）：重拼提示词时不带上，方向就漂了
        "frame": _text(raw.get("frame"), 120),
        # 这一镜动作做完时画面停在哪 —— 既是下一镜的起点，也是这一段视频的尾帧
        "endState": _text(raw.get("endState"), 400),
        # 这一镜发生在哪（「厨房」「卧室」）：少存它，重开画布后「场景：厨房」就凭空消失
        "place": _text(raw.get("place"), 40),
        # 视频节点跨两镜时，动作要「从哪演到哪」：toPlace 是终点那一镜的地点
        "toPlace": _text(raw.get("toPlace"), 40),
        # 全片统一设定（空间基调/时间/光线质感/色调/画质）：整片「看起来是一部片子」的锚
        "setting": _text(raw.get("setting"), 300),
        # 视频节点跨两镜：起点是哪一镜、终点是哪一镜
        "fromShot": round(_num(raw.get("fromShot"), 0, 0, 999)),
        "toShot": round(_num(raw.get("toShot"), 0, 0, 999)),
        # 这一镜有人物参考图：重开画布后改画面重拼提示词时，末尾那句「长相以参考图为准」不能丢
        "refKeep": bool(raw.get("refKeep")),
        # 定妆照是用户自己上传的参考图（不是生成的）：界面上要标出来
        "fromPhoto": bool(raw.get("fromPhoto")),
    }


def sanitize_node(raw, used_ids: set[str]) -> dict | None:
    if not isinstance(raw, dict):
        return None
    node_type = str(raw.get("type") or "")
    if node_type not in NODE_TYPES:
        return None

    node_id = _text(raw.get("id"), 64)
    if not node_id or node_id in used_ids:
        node_id = next_id("nd")
    used_ids.add(node_id)

    node = {
        "id": node_id,
        "type": node_type,
        "x": _num(raw.get("x"), 80, -1e6, 1e6),
        "y": _num(raw.get("y"), 80, -1e6, 1e6),
        "w": _num(raw.get("w"), 260, 140, 1600),
        "h": _num(raw.get("h"), 180, 90, 1600),
        "title": _text(raw.get("title"), 120),
        "text": _text(raw.get("text"), 8000),
        "url": _text(raw.get("url"), 2000),
        "localUrl": _text(raw.get("localUrl"), 2000),
        "localPath": _text(raw.get("localPath"), 1000),
        "fileName": _text(raw.get("fileName"), 300),
        "assetId": _text(raw.get("assetId"), 80),
        # 媒体分辨率是前端量好后存在节点上的（nc-media-dim），不保留每次重开都要重新加载
        "mediaW": round(_num(raw.get("mediaW"), 0, 0, 100000)),
        "mediaH": round(_num(raw.get("mediaH"), 0, 0, 100000)),
        # 手动拉过尺寸的卡：不保留这个标记，重开画布会被 normalizeCardWidths 拉回 210
        "manualSize": bool(raw.get("manualSize")),
        "autoSized": bool(raw.get("autoSized")),
        "gen": sanitize_gen(raw.get("gen")),
    }
    agent = sanitize_agent(raw.get("agent"))
    if agent:
        node["agent"] = agent
    return node


def sanitize_graph(raw) -> dict:
    """整张图的清洗：节点 → 连线 → 视图"""
    raw = raw if isinstance(raw, dict) else {}
    listed = raw.get("nodes") if isinstance(raw.get("nodes"), list) else []
    used_ids: set[str] = set()
    nodes: list[dict] = []
    for item in listed:
        node = sanitize_node(item, used_ids)
        if node:
            nodes.append(node)
        if len(nodes) >= MAX_NODES:
            break

    ids = {n["id"] for n in nodes}
    edges: list[dict] = []
    used_edges: set[str] = set()
    for item in raw.get("edges") if isinstance(raw.get("edges"), list) else []:
        if not isinstance(item, dict):
            continue
        src = str(item.get("from") or "")
        dst = str(item.get("to") or "")
        if src not in ids or dst not in ids or src == dst:
            continue
        edge_id = _text(item.get("id"), 64)
        if not edge_id or edge_id in used_edges:
            edge_id = next_id("eg")
        used_edges.add(edge_id)
        # 连线颜色不保留的话，保存一次就被洗回默认灰
        color = _text(item.get("color"), 24)
        color = color if color in EDGE_COLORS else ""
        edge = {"id": edge_id, "from": src, "to": dst}
        if color:
            edge["color"] = color
        # 连线角色（首帧 / 尾帧 / 普通参考图）：一段视频靠它分清哪张图是起点、哪张是终点。
        # 以前保存一次就洗掉，重开画布只能按连线顺序猜 —— 猜反了就是「首帧当尾帧」。
        role = _text(item.get("role"), 12)
        role = role if role in EDGE_ROLES else ""
        if role:
            edge["role"] = role
        edges.append(edge)
        if len(edges) >= MAX_EDGES:
            break

    viewport = raw.get("viewport") if isinstance(raw.get("viewport"), dict) else {}
    return {
        "nodes": nodes,
        "edges": edges,
        "viewport": {
            "x": _num(viewport.get("x"), 80, -1e6, 1e6),
            "y": _num(viewport.get("y"), 80, -1e6, 1e6),
            "zoom": _num(viewport.get("zoom"), 1, 0.2, 3),
        },
    }


# ============================================================ 聊天记录


def _sanitize_chat_message(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    role = raw.get("role")
    if role not in ("user", "assistant", "error"):
        return None
    body = _text(raw.get("text"), MAX_CHAT_TEXT)
    photos: list[dict] = []
    listed = raw.get("photos") if isinstance(raw.get("photos"), list) else []
    for item in listed[:MAX_CHAT_PHOTOS]:
        if not isinstance(item, dict):
            continue
        photo = {
            "id": _text(item.get("id"), 80),
            "name": _text(item.get("name"), 200),
            "localUrl": _text(item.get("localUrl"), 2000),
        }
        if photo["id"] or photo["localUrl"]:
            photos.append(photo)
    if not body and not photos:
        return None
    msg = {"role": role, "text": body}
    if photos:
        msg["photos"] = photos
    # 这条回复后面挂过「一键生成」按钮：重开画布还得挂上，否则只能重新聊一遍
    if raw.get("action") == "run":
        msg["action"] = "run"
    msg["at"] = round(_num(raw.get("at"), _now(), 0, 1e15))
    return msg


def sanitize_chat(raw) -> list[dict]:
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    # 只留最后 N 条：老对话翻不回去，但把整段剧本留在库里只会把库撑大
    for item in raw[-MAX_CHAT_MESSAGES:]:
        msg = _sanitize_chat_message(item)
        if msg:
            out.append(msg)
    # 再按总字数砍一次：宁可丢最早的几句，也不能让一份存档无限长大
    total = sum(len(m["text"]) for m in out)
    while len(out) > 1 and total > MAX_CHAT_TOTAL:
        total -= len(out[0]["text"])
        out.pop(0)
    return out


def chat_title_of(messages) -> str:
    """对话标题：第一句用户发言（截断），历史列表里靠它认人"""
    for m in messages or []:
        if m.get("role") == "user" and str(m.get("text") or "").strip():
            line = re.sub(r"\s+", " ", str(m["text"])).strip()
            return f"{line[:MAX_CHAT_TITLE]}…" if len(line) > MAX_CHAT_TITLE else line
    return ""


def chat_meta(chat: dict) -> dict:
    """历史列表用：不带整段正文，只给标题 / 条数 / 时间"""
    messages = chat.get("messages") or []
    preview = ""
    for m in reversed(messages):
        if m.get("role") == "assistant":
            preview = str(m.get("text") or "")[:80]
            break
    return {
        "id": chat.get("id"),
        "title": chat.get("title") or chat_title_of(messages) or "一段对话",
        "count": len(messages),
        "preview": preview,
        "createdAt": chat.get("createdAt") or 0,
        "updatedAt": chat.get("updatedAt") or chat.get("createdAt") or 0,
    }


def chats_of(graph: dict) -> list[dict]:
    """老存档迁移：以前一张画布只有一段 `graph.chat`，现在是 `chats[] + chatId`。

    迁移只做一次，做完把旧字段删掉，免得两套数据同时存在互相打架。
    """
    if not isinstance(graph.get("chats"), list):
        legacy = sanitize_chat(graph.get("chat"))
        if legacy:
            at = graph.get("chatAt") or _now()
            graph["chats"] = [
                {
                    "id": next_id("chat"),
                    "title": chat_title_of(legacy),
                    "messages": legacy,
                    "createdAt": at,
                    "updatedAt": at,
                }
            ]
        else:
            graph["chats"] = []
        graph["chatId"] = graph["chats"][0]["id"] if graph["chats"] else None
        graph.pop("chat", None)
        graph.pop("chatAt", None)
        store.mark_dirty("node_canvases")
    return graph["chats"]


def _now() -> int:
    import time

    return int(time.time() * 1000)


# ============================================================ 画布与素材快照


def find_node_canvas(canvas_id: str | None) -> dict | None:
    if not canvas_id:
        return None
    return store.find("node_canvases", id=canvas_id)


def node_canvas_snapshot() -> list[dict]:
    """一整张图的节点列表 + 连线，直接给前端（**数组**，与原版一致）"""
    return list(store.node_canvases or [])


def node_asset_snapshot() -> list[dict]:
    """素材库：回收站里的也一起下发，前端按 deletedAt 分两栏"""
    return [dict(a) for a in (store.node_assets or [])]


def new_graph(name) -> dict:
    at = _now()
    return {
        "id": next_id("ncanvas"),
        "name": str(name or "").strip() or f"节点画布 {len(store.node_canvases or []) + 1}",
        "nodes": [],
        "edges": [],
        "viewport": {"x": 80, "y": 80, "zoom": 1},
        # 编导聊天记录跟着画布走：一张画布可以有多段对话（「新对话」开新的，旧的留在历史里复用）
        "chats": [],
        # 当前正在聊的那段（None = 刚点了「新对话」，还没开口）
        "chatId": None,
        "createdAt": at,
        "updatedAt": at,
    }


def migrate_legacy_chats() -> bool:
    """开机先把老存档的 `graph.chat` 升级成 `chats[]`：只在内存里改，有改动才落盘一次"""
    migrated = False
    for graph in store.node_canvases or []:
        if not isinstance(graph.get("chats"), list):
            chats_of(graph)
            migrated = True
    if migrated:
        store.save()
        store.flush()
    return migrated


def find_node_asset(asset_id: str | None) -> dict | None:
    if not asset_id:
        return None
    for asset in store.node_assets or []:
        if str(asset.get("id")) == str(asset_id):
            return asset
    return None


def _asset_path_key(asset: dict) -> str:
    return os.path.abspath(str(asset.get("localPath") or ""))


def _new_asset_id() -> str:
    return next_id("na")


# ============================================================ 路由层（app/api/node_canvas.py 用）

def create_graph(name=None) -> dict:
    graph = new_graph(name)
    rows = store.node_canvases
    rows.append(graph)
    store.mark_dirty("node_canvases")
    store.save()
    store.flush()
    return graph


def update_graph(graph: dict, body: dict) -> dict:
    """保存整张图：前端自动保存走这里，一次把 nodes / edges / viewport 全量提交"""
    if body.get("name") is not None:
        graph["name"] = _text(body.get("name"), 120).strip() or graph.get("name")
    if body.get("nodes") is not None or body.get("edges") is not None:
        clean = sanitize_graph(
            {
                "nodes": body.get("nodes") if body.get("nodes") is not None else graph.get("nodes"),
                "edges": body.get("edges") if body.get("edges") is not None else graph.get("edges"),
                "viewport": body.get("viewport") if body.get("viewport") is not None else graph.get("viewport"),
            }
        )
        graph["nodes"] = clean["nodes"]
        graph["edges"] = clean["edges"]
        graph["viewport"] = clean["viewport"]
    elif body.get("viewport") is not None:
        clean = sanitize_graph({"nodes": graph.get("nodes"), "edges": graph.get("edges"), "viewport": body.get("viewport")})
        graph["viewport"] = clean["viewport"]
    graph["updatedAt"] = _now()
    store.mark_dirty("node_canvases")
    store.save()
    store.flush()
    return graph


def delete_graph(graph: dict) -> None:
    rows = store.node_canvases
    rows.remove(graph)
    store.mark_dirty("node_canvases")
    store.save()
    store.flush()


def save_chat(graph: dict, body: dict) -> dict:
    """编导聊天记录：一张画布下可以有多段对话，业务是「upsert 某一段」"""
    chats = chats_of(graph)
    messages = sanitize_chat(body.get("messages"))
    wanted = _text(body.get("chatId"), 64)
    chat = next((c for c in chats if c.get("id") == wanted), None) if wanted else None

    if not chat and not messages:
        # 「新对话」= 只把指针清掉。新建一条空记录的话，历史里会堆一堆点开啥也没有的空壳
        graph["chatId"] = None
        store.mark_dirty("node_canvases")
        store.save()
        store.flush()
        return {"ok": True, "chatId": None, "sessions": [chat_meta(c) for c in chats]}

    if not chat:
        at = _now()
        chat = {"id": next_id("chat"), "title": "", "messages": [], "createdAt": at, "updatedAt": at}
        chats.append(chat)
    chat["messages"] = messages
    chat["title"] = chat_title_of(messages) or chat.get("title") or ""
    chat["updatedAt"] = _now()
    graph["chatId"] = chat["id"]

    # 超过上限就从最久没动过的那段开始丢（当前这段永远保留）
    if len(chats) > MAX_CHATS:
        stale = sorted((c for c in chats if c.get("id") != chat["id"]), key=lambda c: c.get("updatedAt") or 0)
        while len(chats) > MAX_CHATS and stale:
            dropped = stale.pop(0)
            if dropped in chats:
                chats.remove(dropped)

    store.mark_dirty("node_canvases")
    store.save()
    store.flush()
    return {
        "ok": True,
        "chatId": chat["id"],
        "chat": chat_meta(chat),
        "sessions": [chat_meta(c) for c in chats],
    }


def delete_chat(graph: dict, chat_id: str) -> dict:
    """删掉一段历史对话（画布上的节点不动）"""
    chats = chats_of(graph)
    chat = next((c for c in chats if c.get("id") == chat_id), None)
    if not chat:
        raise LookupError("这段对话不存在")
    chats.remove(chat)
    if graph.get("chatId") == chat_id:
        graph["chatId"] = None
    store.mark_dirty("node_canvases")
    store.save()
    store.flush()
    return {"ok": True, "sessions": [chat_meta(c) for c in chats]}


def export_graph(graph: dict) -> dict:
    """导出：文件里不带 id，换台机器导入也不会撞号"""
    return {
        "format": "lumen-forge-node-canvas",
        "version": 1,
        "name": graph.get("name"),
        "nodes": graph.get("nodes"),
        "edges": graph.get("edges"),
        "viewport": graph.get("viewport"),
        "exportedAt": _iso_now(),
    }


def export_file_name(graph: dict) -> str:
    safe = _MEDIA_NAME_RE.sub("_", str(graph.get("name") or "")) or "node-canvas"
    return f"{safe}.json"


def import_graph(raw) -> dict:
    """导入：整份 JSON 变成一张新画布，不覆盖现有的"""
    payload = raw.get("graph") if isinstance(raw, dict) and raw.get("graph") is not None else raw
    if not isinstance(payload, dict):
        raise ValueError("文件内容不是有效的画布 JSON")
    clean = sanitize_graph(payload)
    if not clean["nodes"]:
        raise ValueError("这份 JSON 里没有可用的节点")
    graph = new_graph(_text(payload.get("name"), 120) or "导入的画布")
    graph["nodes"] = clean["nodes"]
    graph["edges"] = clean["edges"]
    graph["viewport"] = clean["viewport"]
    rows = store.node_canvases
    rows.append(graph)
    store.mark_dirty("node_canvases")
    store.save()
    store.flush()
    return graph


def _iso_now() -> str:
    from datetime import datetime

    return datetime.now().astimezone().isoformat(timespec="milliseconds")


# ------------------------------------------------------------ 本地素材库


def register_upload(buffer: bytes, original_name: str, mime: str = "", paths: Paths | None = None) -> dict:
    """上传素材：与画布素材一样走原始二进制（不引入 multipart 依赖）"""
    if not buffer:
        raise ValueError("没有收到文件内容")
    original_name = original_name or "素材"
    ext = Path(original_name).suffix.lower()
    kind = ext_kind(ext)
    if not kind:
        raise ValueError("只支持图片（png/jpg/webp/gif…）、视频（mp4/mov/webm…）、音频（mp3/wav/m4a…）")

    asset_id = _new_asset_id()
    stored_name = f"{asset_id}{ext}"
    upload_dir = Path(paths.uploads)
    try:
        upload_dir.mkdir(parents=True, exist_ok=True)
        local_path = upload_dir / stored_name
        local_path.write_bytes(buffer)
    except OSError as err:
        raise RuntimeError(f"写入素材目录失败：{err}") from err

    asset = {
        "id": asset_id,
        "kind": kind,
        "fileName": stored_name,
        "originalName": original_name or stored_name,
        "localPath": str(local_path),
        "localUrl": f"/files/uploads/{stored_name}",
        "mimeType": mime or KIND_MIME[kind],
        "size": len(buffer),
        "favorite": False,
        "deletedAt": None,
        "createdAt": _now(),
    }
    rows = store.node_assets
    rows.append(asset)
    store.mark_dirty("node_assets")
    store.save()
    store.flush()
    return asset


def register_existing(body: dict, paths: Paths) -> dict:
    """把「已经在磁盘上的文件」登记进素材库（不复制）。

    用途：画布节点上的生成结果 / 从别处引用的文件，点「存入素材库」时走这里。
    安全闸：只接受七个媒体目录或数据目录以内的文件，并且必须真实存在。
    """
    # 生成结果节点只有 /files/... 的 URL，没有 localPath：按 URL 反推它真实所在的目录
    from_url = path_from_files_url(body.get("localUrl") or "", paths)
    raw = body.get("localPath") or (str(from_url) if from_url else "")
    if not raw:
        raise ValueError("只允许登记本程序管理的目录里的文件")
    abs_path = Path(os.path.abspath(str(raw)))
    if not is_allowed_register_path(abs_path, paths):
        raise ValueError("只允许登记本程序管理的目录里的文件")
    if not abs_path.exists():
        raise ValueError("文件不存在，无法登记")
    if not abs_path.is_file():
        raise ValueError("目标的不是一个文件")

    key = abs_path.as_posix()
    for asset in store.node_assets or []:
        if _asset_path_key(asset) == key:
            return asset

    ext = abs_path.suffix.lower()
    kind = ext_kind(ext) or str(body.get("kind") or "image")
    if kind not in KIND_EXTS:
        raise ValueError("无法识别的文件类型")
    asset = {
        "id": _new_asset_id(),
        "kind": kind,
        "fileName": abs_path.name,
        "originalName": _text(body.get("fileName"), 300) or abs_path.name,
        "localPath": str(abs_path),
        # 按「它实际在哪个媒体目录里」拼 URL，改过存储位置后也能正常访问
        "localUrl": files_url_for(abs_path, paths),
        "mimeType": KIND_MIME[kind],
        "size": abs_path.stat().st_size,
        "favorite": False,
        "deletedAt": None,
        "shared": True,
        "createdAt": _now(),
    }
    rows = store.node_assets
    rows.append(asset)
    store.mark_dirty("node_assets")
    store.save()
    store.flush()
    return asset


def asset_to_image_ref(asset: dict) -> dict:
    """素材库里的图直接当人物参考图。

    不再从磁盘复制一份：素材文件本来就是持久的，引用它即可。
    同一份素材反复当参考图很常见，已经登记过就返回原来那条，别每次点都往 image_refs 里堆。
    """
    from .image_jobs import create_ref_from_existing_file

    if asset.get("kind") != "image":
        raise ValueError("只有图片能当人物参考图")
    for ref in store.image_refs or []:
        if str(ref.get("localPath") or "") == str(asset.get("localPath") or ""):
            return ref
    ref = create_ref_from_existing_file(
        local_path=asset.get("localPath") or "",
        original_name=asset.get("originalName") or "",
        mime_type=asset.get("mimeType") or "",
        size=asset.get("size") or 0,
    )
    # 素材住在 uploads/ 下，而 create_ref_from_existing_file 只认得 materials/ 和 refs/，
    # 会给出一个 404 的 localUrl —— 直接用素材自己的地址覆盖掉
    ref["localUrl"] = asset.get("localUrl") or ref.get("localUrl")
    store.mark_dirty("image_refs")
    store.save()
    store.flush()
    return ref


def asset_lists() -> dict:
    rows = store.node_assets or []
    return {
        "items": [a for a in rows if not a.get("deletedAt")],
        "trash": [a for a in rows if a.get("deletedAt")],
    }


def patch_asset(asset: dict, body: dict) -> dict:
    if body.get("favorite") is not None:
        asset["favorite"] = bool(body.get("favorite"))
    if body.get("name") is not None:
        asset["originalName"] = _text(body.get("name"), 300).strip() or asset.get("originalName")
    store.mark_dirty("node_assets")
    store.save()
    store.flush()
    return asset


def trash_asset(asset: dict) -> dict:
    """删除 = 先进回收站，文件不动；真要删在回收站里点「永久删除」"""
    asset["deletedAt"] = _now()
    store.mark_dirty("node_assets")
    store.save()
    store.flush()
    return {"ok": True}


def restore_asset(asset: dict) -> dict:
    asset["deletedAt"] = None
    store.mark_dirty("node_assets")
    store.save()
    store.flush()
    return asset


def purge_asset(asset: dict) -> dict:
    rows = store.node_assets
    rows.remove(asset)
    # 先摘记录再删：节点画布里如果还引用着这份文件，就留着
    unlink_if_unused(asset.get("localPath") or "")
    store.mark_dirty("node_assets")
    store.save()
    store.flush()
    return {"ok": True}


def clear_trash() -> dict:
    rows = store.node_assets
    trashed = [a for a in rows if a.get("deletedAt")]
    keep = [a for a in rows if not a.get("deletedAt")]
    store.replace("node_assets", keep)
    removed = 0
    for asset in trashed:
        if unlink_if_unused(asset.get("localPath") or ""):
            removed += 1
    store.save()
    store.flush()
    return {"ok": True, "count": len(trashed), "removed": removed}
