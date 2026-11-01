"""素材模块 —— 逐函数复刻 `server/materials.js`（44999 字节 / 1088 行）。

## 这块在管什么

用户配「分类」（= 一条接口），每个分类能拉一批随机素材出来，看中了就
「下载到本地」或「加进画布 / 加参考图」。所以这里有四件事：

1. **拉列表**：三种模式 —— `api`（接口返回 JSON/纯文本列表）、`m3u8`（随机短片线路池，
   一个地址就是一段片子）、`redirect`（302 跳到随机直链）。三种模式的去重逻辑完全不同。
2. **线路池**：一个分类可以配多条线路，按顺序试，第一条通了就用它，并记住（下次优先）。
3. **下载落盘**：m3u8 要「拉播放列表 → 逐片下载 → 依序合并」，其余直存。
4. **图源检索**：关键词搜图（360 / Pixiv / 动漫标签站 / 必应）+ pixiv 排行榜。

## 移植时几个必须照抄的设计（改了就会出问题）

- **「200 直出」接口要落快照**：像 `api.yujn.cn/api/jk.php` 这类不跳转、直接把图片吐回来的
  接口，地址永远是接口本身。若直接把它当媒体地址，每条素材 URL 都一样，浏览器按同一地址
  缓存，翻来翻去只能看到第一张。所以这一份内容要抓下来存内存，给一个本地地址
  （`/api/materials/rand/<token>`），显示和下载都指向同一份。
- **内容指纹 `material_key`**：抖音那条链路的同一段片子换个签名 / 换个 CDN 节点，
  整条 URL 就长得不一样；只按 URL 去重会认不出是同一份。带 `video_id` / `item_id` /
  `aweme_id` 的直接用这个 id，其余用「域名 + 路径」并去掉最前面那节 CDN 节点名。
- **「看过什么」只存内存**：每个分类记最近 600 个指纹，凑满一批时优先给没看过的。
  不落库 —— 重启后重来一遍无所谓，别往库里塞这种临时数据。
- **`unique` 下载**：给「每次请求都是另一段随机短片」的线路用，文件名带时间戳，
  否则同一线路下载多次会互相覆盖，只剩最后一段。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin, urlparse, urlunparse

import httpx

from .config import DATA_DIR
from .models import normalize_line_pool, normalize_material_lines
from .scheduler import get_paths, mask_key
from .store import store

log = logging.getLogger(__name__)

FETCH_TIMEOUT_MS = 20000
DOWNLOAD_TIMEOUT_MS = 60000
CACHE_TTL_MS = 3 * 60 * 1000

#: 会带 Referer 的下载：很多图床有防盗链
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

VIDEO_EXTS = ["mp4", "mov", "webm", "m3u8"]
IMAGE_EXTS = ["png", "jpg", "jpeg", "webp", "gif"]
MEDIA_EXTS = {"video": VIDEO_EXTS, "image": IMAGE_EXTS}

MIME_BY_EXT = {
    "mp4": "video/mp4",
    "mov": "video/quicktime",
    "webm": "video/webm",
    "m3u8": "application/vnd.apple.mpegurl",
    "ts": "video/mp2t",
    "m4s": "video/mp4",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "gif": "image/gif",
}

MIME_TO_EXT = {
    "video/mp4": "mp4",
    "video/quicktime": "mov",
    "video/webm": "webm",
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
}

#: 字段名里带这些词的，多半是带水印的版本：挑地址时优先跳过
WATERMARK_HINT = re.compile(r"water|mark|wm|logo", re.I)

#: 按 kind 的字段名优先顺序：越靠前越像「播放用的原片」
URL_KEYS_BY_KIND = {
    "video": [
        "play_url",
        "video_url",
        "playurl",
        "video",
        "play",
        "url",
        "src",
        "file",
        "download_url",
        "link",
        "href",
    ],
    "image": ["image_url", "imgurl", "img", "image", "pic", "picture", "url", "src", "link", "href"],
}

#: 视频封面 / 缩略图的候选字段
POSTER_KEYS = [
    "cover",
    "cover_url",
    "thumb",
    "thumbnail",
    "poster",
    "first_frame",
    "origin_cover",
    "image",
    "img",
    "pic",
]

#: 标题的候选字段
TITLE_KEYS = ["title", "name", "desc", "description", "caption", "text", "author"]

#: 拉列表结果缓存：source.id → (at, items, line)
_cache: dict[str, tuple[float, list[dict], str]] = {}

# ------------------------------------------------------------------ 快照

#: token → {buffer, mime, at}
_snapshots: dict[str, dict[str, Any]] = {}
SNAPSHOT_TTL_MS = 30 * 60 * 1000
SNAPSHOT_MAX = 120
SNAPSHOT_MAX_BYTES = 8 * 1024 * 1024

#: 本机服务基址（后端要按 URL 再取一次素材时，把本地相对地址补全成绝对地址）
_local_base = ""


def set_material_local_base(base: str) -> None:
    """由 main.py 在监听成功后注入实际端口（端口是 0 随机分配的，配置里写不死）"""
    global _local_base
    _local_base = str(base or "").rstrip("/")


def _prune_snapshots() -> None:
    now = time.time() * 1000
    for token in list(_snapshots.keys()):
        if now - _snapshots[token]["at"] > SNAPSHOT_TTL_MS:
            _snapshots.pop(token, None)
    while len(_snapshots) > SNAPSHOT_MAX:
        _snapshots.pop(next(iter(_snapshots)), None)


def read_material_snapshot(token: str) -> dict | None:
    """取回「200 直出」接口落下的那一份内容（过期 / 不存在都返回 None）"""
    snap = _snapshots.get(str(token or ""))
    if not snap:
        return None
    if time.time() * 1000 - snap["at"] > SNAPSHOT_TTL_MS:
        _snapshots.pop(str(token), None)
        return None
    return snap


# ------------------------------------------------------------------ 分类


def find_material_source(source_id: str | None) -> dict | None:
    if not source_id:
        return None
    return store.find("material_sources", id=source_id)


def invalidate_material_items(source_id: str) -> None:
    """改了分类配置（地址 / 解析参数）后作废缓存，下一次请求立刻走新配置"""
    _cache.pop(source_id, None)
    _seen_keys.pop(source_id, None)  # 「看过什么」也一起忘掉：配置都换了，从头开始


def invalidate_all_material_items() -> None:
    """全局线路池变了：所有分类的缓存都作废（拼出来的线路跟着变）"""
    _cache.clear()
    _seen_keys.clear()


def own_lines(source: dict) -> list[str]:
    """分类自己填的线路（老 url 兜底）：不含全局线路池拼出来的"""
    own = normalize_material_lines(source.get("lines"))
    legacy = str(source.get("url") or "").strip()
    if legacy and legacy not in own:
        own.insert(0, legacy)
    return own


def material_lines(source: dict) -> list[str]:
    """分类实际可用的线路。

    - 普通接口：就是分类自己填的 lines（老 url 兜底）
    - 随机短片线路池：`全局线路池基地址 + 分类编号` 拼出来的线路排在前面
      （照搬原 App：线路域名集中管理，每个分类只有一个编号），分类自己填的完整地址留作兜底
    """
    own = own_lines(source)
    if source.get("mode") != "m3u8":
        return own
    clip = str(source.get("clip") or "").strip()
    if not is_clip_id(clip):
        return own
    built = [f"{base}/{clip}.m3u8" for base in normalize_line_pool(store.material_line_pool)]
    out: list[str] = []
    for line in [*built, *own]:
        if line not in out:
            out.append(line)
    return out


def _ordered_lines(source: dict) -> list[str]:
    """上次能用的那条排最前，其余按配置顺序（对应原 App 的「记住上次可用线路」）"""
    lines = material_lines(source)
    last = str(source.get("lastGoodLine") or "").strip()
    if last not in lines:
        return lines
    index = lines.index(last)
    if index <= 0:
        return lines
    return [lines[index], *lines[:index], *lines[index + 1 :]]


def _host_of(url: str) -> str:
    try:
        parsed = urlparse(str(url))
        return parsed.netloc or str(url)
    except Exception:  # noqa: BLE001 - 认不出来就原样回
        return str(url)


def material_headers(source: dict) -> dict[str, str]:
    """请求头（列表接口和下载都用它）：很多 m3u8 节点校验 Referer/UA，不加就直接 403"""
    headers = {"User-Agent": UA, **_parse_json_object(source.get("headers"), "请求头")}
    api_key = str(source.get("apiKey") or "").strip()
    if api_key:
        header_name = str(source.get("apiKeyHeader") or "").strip() or "Authorization"
        headers[header_name] = f"{source.get('apiKeyPrefix') or ''}{api_key}"
    return headers


def material_source_snapshot(source: dict) -> dict:
    """对外快照：API Key 只回显掩码，避免整串密钥反复在网络上传输"""
    out = {k: v for k, v in source.items() if k != "url"}  # url 是 0.7.13 的老字段，已并进 lines
    own = own_lines(source)
    out["lines"] = own
    out["builtLines"] = (
        [line for line in material_lines(source) if line not in own]
        if source.get("mode") == "m3u8"
        else []
    )
    out["apiKey"] = mask_key(source.get("apiKey"))
    out["hasKey"] = bool(source.get("apiKey"))
    return out


# ------------------------------------------------------------------ 小工具


def _parse_json_object(text, label: str) -> dict:
    """解析用户填的 JSON 对象；空值给 {}，非法值抛人话错误"""
    raw = str(text or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as err:
        raise ValueError(f"{label}不是合法的 JSON：{err}") from err
    if not isinstance(parsed, dict):
        raise ValueError(f'{label}必须是一个 JSON 对象，例如 {{"a": 1}}')
    return parsed


def _get_by_path(node, dotted: str):
    """按点路径取值，支持数组下标，如 data.list / data.0.url"""
    cur = node
    for part in str(dotted).split("."):
        if cur is None:
            return None
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
            continue
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _find_object_arrays(node, depth: int = 0, out: list | None = None) -> list[dict]:
    """递归找出所有「元素是对象的数组」，用于 itemsPath 没填时自动推断"""
    if out is None:
        out = []
    if not isinstance(node, (dict, list)):
        return out
    if isinstance(node, list):
        if node and all(isinstance(it, dict) for it in node):
            out.append({"arr": node, "depth": depth})
        for it in node:
            _find_object_arrays(it, depth + 1, out)
        return out
    for value in node.values():
        _find_object_arrays(value, depth + 1, out)
    return out


def _collect_strings(node, out: list[str] | None = None) -> list[str]:
    """递归收集所有字符串值"""
    if out is None:
        out = []
    if isinstance(node, str):
        out.append(node)
    elif isinstance(node, list):
        for it in node:
            _collect_strings(it, out)
    elif isinstance(node, dict):
        for value in node.values():
            _collect_strings(value, out)
    return out


def _is_http_url(value) -> bool:
    return isinstance(value, str) and bool(re.match(r"^https?://\S+$", value.strip(), re.I))


def _ext_of(url: str) -> str:
    """从 URL 路径里取扩展名（去掉 query），取不到返回空串"""
    try:
        path = urlparse(str(url).strip()).path
    except Exception:  # noqa: BLE001
        return ""
    ext = path.rsplit("/", 1)[-1]
    if "." not in ext:
        return ""
    ext = ext.rsplit(".", 1)[-1].lower()
    return ext if re.match(r"^[a-z0-9]{2,5}$", ext) else ""


def _is_media_url(url: str, kind: str) -> bool:
    ext = _ext_of(url)
    return bool(ext) and ext in (MEDIA_EXTS.get(kind) or [])


# ------------------------------------------------------------------ 内容指纹

#: 带这些参数的地址直接用这个 id 当指纹（抖音中转地址就长这样）
ID_PARAMS = ["video_id", "item_id", "aweme_id"]


def material_key(raw_url) -> str:
    """把 URL 折算成一个稳定的内容指纹。

    同一条视频换个签名（抖音那个 sign）、换个 CDN 节点（alimov2 ↔ txmov2），
    整条 URL 就长得不一样了；只按 URL 去重会认不出是同一份，一批里就会混进重复的。
    """
    raw = str(raw_url or "").strip()
    try:
        parsed = urlparse(raw)
    except Exception:  # noqa: BLE001
        return raw  # 本机相对地址（/api/materials/rand/xxx）没法拆，原样当指纹
    if not parsed.scheme and not parsed.netloc:
        return raw
    from urllib.parse import parse_qs

    query = parse_qs(parsed.query)
    for name in ID_PARAMS:
        value = (query.get(name) or [""])[0]
        if value:
            return f"id:{name}={value}"
    labels = parsed.netloc.split(".")
    host = ".".join(labels[1:]) if len(labels) >= 4 else parsed.netloc
    return f"{host}{parsed.path}"


def _shuffled(items: list) -> list:
    """洗牌：只在「一次给一批」的地方用，不动远端清单本身"""
    out = list(items)
    for i in range(len(out) - 1, 0, -1):
        j = random.randint(0, i)
        out[i], out[j] = out[j], out[i]
    return out


#: 每个分类记一份「最近给过的内容指纹」。只存内存、不落库
_seen_keys: dict[str, dict[str, Any]] = {}
SEEN_MAX = 600


def _seen_for(source: dict) -> dict[str, Any]:
    rec = _seen_keys.get(source["id"])
    if not rec:
        rec = {"keys": set(), "order": []}
        _seen_keys[source["id"]] = rec
    return rec


def _remember_seen(source: dict, keys: list[str]) -> None:
    rec = _seen_for(source)
    for key in keys:
        if not key or key in rec["keys"]:
            continue
        rec["keys"].add(key)
        rec["order"].append(key)
    # 只记最近这么多条，更早的忘掉，免得清单大时记忆无限涨
    while len(rec["order"]) > SEEN_MAX:
        rec["keys"].discard(rec["order"].pop(0))


def _forget_seen(source: dict) -> None:
    _seen_keys.pop(source["id"], None)


# ------------------------------------------------------------------ 解析列表


def _pick_url(item: dict, kind: str, field: str) -> str:
    """从一项里挑出媒体地址：优先用户指定的字段，其次按候选键顺序（跳过疑似带水印的字段）"""
    if field:
        value = _get_by_path(item, field)
        # 路径没取到就兼容一下「字段名直接是键」的情况
        direct = value if _is_http_url(value) else item.get(field)
        return direct.strip() if _is_http_url(direct) else ""
    keys = URL_KEYS_BY_KIND.get(kind) or URL_KEYS_BY_KIND["video"]
    entries = [(str(k), v) for k, v in item.items()]

    def scan(allow_watermark: bool) -> str:
        for want in keys:
            for key, value in entries:
                if key.lower() != want:
                    continue
                if not allow_watermark and WATERMARK_HINT.search(key):
                    continue
                if _is_http_url(value):
                    return value.strip()
        return ""

    hit = scan(False) or scan(True)
    if hit:
        return hit
    # 兜底：同项里第一个扩展名对得上的链接
    for text in _collect_strings(item):
        if _is_media_url(text, kind):
            return text
    return ""


def _pick_by_keys(item: dict, keys: list[str]) -> str:
    for want in keys:
        for key, value in item.items():
            if str(key).lower() != want:
                continue
            if _is_http_url(value):
                return value.strip()
    return ""


def _pick_poster(item: dict) -> str:
    return _pick_by_keys(item, POSTER_KEYS)


def _pick_title(item: dict) -> str:
    for want in TITLE_KEYS:
        for key, value in item.items():
            if str(key).lower() != want:
                continue
            if isinstance(value, str) and value.strip():
                return value.strip()[:80]
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return str(value)
    return ""


def parse_items(text: str, source: dict) -> list[dict]:
    """把任意返回体解析成统一的素材列表"""
    kind = "image" if source.get("kind") == "image" else "video"
    items: list[dict] = []
    seen: set[str] = set()

    def push(url, poster: str = "", title: str = "") -> None:
        if not _is_http_url(url):
            return
        link = url.strip()
        # 按内容指纹去重：同一份素材换个 CDN 地址（签名不同）也只留一条
        key = material_key(link)
        if key in seen:
            return
        seen.add(key)
        ext = _ext_of(link)
        # hls：前端要用 hls.js 挂载播放（Chromium 自己不会播 m3u8）
        items.append(
            {
                "id": link,
                "url": link,
                "poster": poster,
                "title": title,
                "ext": ext,
                "hls": ext == "m3u8",
            }
        )

    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        data = None

    # 不是 JSON（很多老接口直接吐一行一个链接）就直接抓链接
    if data is None:
        for match in re.finditer(r"""https?://[^\s"'<>)\]]+""", str(text)):
            if _is_media_url(match.group(0), kind):
                push(match.group(0))
        return items

    items_path = str(source.get("itemsPath") or "").strip()
    field = str(source.get("urlField") or "").strip()
    listing: list = []
    if items_path:
        got = _get_by_path(data, items_path)
        listing = got if isinstance(got, list) else [got] if isinstance(got, dict) else []
    else:
        candidates = _find_object_arrays(data)
        if candidates:
            # 取元素最多的那一层；层数相同时取靠外的
            candidates.sort(key=lambda c: (-len(c["arr"]), c["depth"]))
            listing = candidates[0]["arr"]
        elif isinstance(data, list):
            listing = data

    for item in listing:
        if isinstance(item, str):
            if _is_media_url(item, kind):
                push(item)
            continue
        if not isinstance(item, dict):
            continue
        url = _pick_url(item, kind, field)
        if url:
            push(url, _pick_poster(item), _pick_title(item))

    # 兜底：整段 JSON 里扫一遍扩展名对得上的链接（有些接口把地址塞在很偏的地方）
    if not items:
        for text_value in _collect_strings(data):
            if _is_media_url(text_value, kind):
                push(text_value)
    return items


# ------------------------------------------------------------------ HLS(m3u8)

#: 响应类型是 m3u8：有些地址不带 .m3u8 后缀，只能靠 content-type 认出来
M3U8_MIME = re.compile(r"(application/(vnd\.apple\.mpegurl|x-mpegurl)|audio/mpegurl)", re.I)
SEGMENT_TIMEOUT_MS = 20000
#: 整条 HLS 合并下载的总时间预算：分片多的时候 60 秒不够
HLS_BUDGET_MS = 180000
HLS_MAX_SEGMENTS = 3000


def _is_m3u8_url(url: str) -> bool:
    return _ext_of(url) == "m3u8"


def _resolve_uri(uri: str, base_url: str) -> str:
    """播放列表里的相对地址（分片、变体、init）都按播放列表自身地址解析"""
    try:
        return urljoin(base_url, str(uri).strip())
    except Exception:  # noqa: BLE001
        return ""


async def fetch_text(url: str, headers: dict, timeout_ms: int, label: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=timeout_ms / 1000) as client:
            res = await client.get(url, headers=headers)
    except httpx.TimeoutException as err:
        raise ValueError(f"{label}：超时（{round(timeout_ms / 1000)} 秒没响应）") from err
    except httpx.HTTPError as err:
        raise ValueError(f"{label}：{err}") from err
    text = res.text
    if res.status_code >= 400:
        raise ValueError(f"{label}：HTTP {res.status_code}{f'：{text[:120]}' if text else ''}")
    return text


def parse_playlist(text: str, base_url: str) -> dict:
    """拆 m3u8：master 列表里挑一路变体，media 列表里拿到分片清单。

    加密流（#EXT-X-KEY 的 METHOD 不是 NONE）没法直接拼成文件，直接报错。
    """
    body = str(text or "")
    if not re.search(r"#EXTM3U", body):
        raise ValueError("拿到的不是 m3u8 播放列表（缺少 #EXTM3U）")
    if re.search(r"#EXT-X-KEY:[^\n]*METHOD=(?!NONE)", body, re.I):
        raise ValueError("这个 HLS 流带加密（#EXT-X-KEY），没法直接合并成文件")

    lines = [line.strip() for line in re.split(r"\r?\n", body)]
    variants: list[dict] = []
    segments: list[str] = []
    init_segment = ""

    i = 0
    while i < len(lines):
        line = lines[i]
        if not line:
            i += 1
            continue
        if re.match(r"^#EXT-X-MAP:", line, re.I):
            match = re.search(r'URI="([^"]+)"', line, re.I)
            if match:
                init_segment = _resolve_uri(match.group(1), base_url)
            i += 1
            continue
        if re.match(r"^#EXT-X-STREAM-INF:", line, re.I):
            j = i + 1
            while j < len(lines) and (not lines[j] or lines[j].startswith("#")):
                j += 1
            if j < len(lines):
                bandwidth = re.search(r"BANDWIDTH=(\d+)", line, re.I)
                variants.append(
                    {
                        "url": _resolve_uri(lines[j], base_url),
                        "bandwidth": int(bandwidth.group(1)) if bandwidth else 0,
                    }
                )
                i = j
            i += 1
            continue
        if line.startswith("#"):
            i += 1
            continue
        segments.append(_resolve_uri(line, base_url))
        i += 1

    return {
        "variants": [v for v in variants if v["url"]],
        "segments": [s for s in segments if s],
        "initSegment": init_segment,
        "live": not re.search(r"#EXT-X-ENDLIST", body, re.I),
    }


async def _probe_playlist(url: str, headers: dict) -> dict:
    """只验证「这个地址能返回一份可用的播放列表」，不下载分片（拉列表时用来探活）"""
    listing = parse_playlist(
        await fetch_text(url, headers, FETCH_TIMEOUT_MS, "拉取播放列表失败"), url
    )
    if not listing["segments"] and not listing["variants"]:
        raise ValueError("播放列表是空的（地址可能已失效）")
    return listing


async def _merge_hls(playlist_url: str, headers: dict, playlist_text: str) -> dict:
    """逐片下载再按顺序合并。播放列表本身只有几 KB 文本，直下没有任何意义。

    分片是 fMP4（有 #EXT-X-MAP 或 .m4s 后缀）就存 .mp4，否则存 .ts。
    """
    listing = parse_playlist(playlist_text, playlist_url)
    if not listing["segments"] and listing["variants"]:
        best = sorted(listing["variants"], key=lambda v: -v["bandwidth"])[0]
        listing = parse_playlist(
            await fetch_text(best["url"], headers, FETCH_TIMEOUT_MS, "拉取播放列表失败"),
            best["url"],
        )
    if not listing["segments"]:
        raise ValueError("播放列表里没有分片（可能是直播流，或地址已失效）")
    if len(listing["segments"]) > HLS_MAX_SEGMENTS:
        raise ValueError(f"分片太多（{len(listing['segments'])} 个），不像短视频，放弃下载")

    urls = (
        [listing["initSegment"], *listing["segments"]]
        if listing["initSegment"]
        else list(listing["segments"])
    )
    chunks: list[bytes] = []
    started_at = time.monotonic()
    for segment in urls:
        if (time.monotonic() - started_at) * 1000 > HLS_BUDGET_MS:
            raise ValueError("合并下载超时（分片太多或网速太慢），换个线路再试")
        try:
            async with httpx.AsyncClient(timeout=SEGMENT_TIMEOUT_MS / 1000) as client:
                res = await client.get(segment, headers=headers)
        except httpx.TimeoutException as err:
            raise ValueError("下载分片失败：超时") from err
        except httpx.HTTPError as err:
            raise ValueError(f"下载分片失败：{err}") from err
        if res.status_code >= 400:
            raise ValueError(f"下载分片失败：HTTP {res.status_code}")
        chunks.append(res.content)

    buffer = b"".join(chunks)
    if not buffer:
        raise ValueError("合并出来的文件是空的")
    fragmented = bool(listing["initSegment"]) or bool(
        re.search(r"\.(m4s|mp4)(\?|$)", urls[-1] if urls else "", re.I)
    )
    return {"buffer": buffer, "ext": "mp4" if fragmented else "ts"}


# ------------------------------------------------------------------ 线路池

CLIP_ID_RE = re.compile(r"^[\w.-]{1,64}$")


def is_clip_id(clip) -> bool:
    """分类编号合法性：只允许字母数字和 - _ .（要拼进 URL 路径段）"""
    return bool(CLIP_ID_RE.match(str(clip or "").strip()))


def _absolute_line(raw_url: str) -> str:
    """线路地址可能是「本机相对路径」（如 /material-lists/shaofu.txt）：

    桌面版端口是随机分配的，配置里不可能写死，所以按实际端口补成绝对地址再请求。
    """
    raw = str(raw_url or "").strip()
    if raw.startswith("/") and _local_base:
        return f"{_local_base}{raw}"
    return raw


def _with_extra_params(raw_url: str, source: dict) -> str:
    """把「额外参数」并进地址"""
    try:
        parsed = urlparse(raw_url)
    except Exception as err:  # noqa: BLE001
        raise ValueError("地址不是合法的 URL，请填 http(s) 开头的完整地址") from err
    if parsed.scheme.lower() not in ("http", "https"):
        raise ValueError("地址必须是 http(s) 开头的地址")
    extra = _parse_json_object(source.get("extraParams"), "额外参数")
    if extra:
        from urllib.parse import parse_qsl, urlencode

        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        for key, value in extra.items():
            query[key] = "" if value is None else str(value)
        parsed = parsed._replace(query=urlencode(query, quote_via=quote))
    return urlunparse(parsed)


async def _try_lines(source: dict, attempt) -> dict:
    """按线路池依次尝试：第一条成功的就用，并记下来（下次优先）。

    attempt 抛错 = 这条线路不可用，自动换下一条；全部失败则汇总报错。
    """
    lines = _ordered_lines(source)
    if not lines:
        if source.get("mode") == "m3u8":
            raise ValueError(
                f"分类「{source.get('name')}」拼不出线路：到「接口管理」"
                "把全局线路池（基地址）和这个分类的编号都填上"
            )
        raise ValueError(f"分类「{source.get('name')}」还没填接口地址，点「接口管理」补上")

    failures: list[str] = []
    for line in lines:
        try:
            value = await attempt(line)
        except Exception as err:  # noqa: BLE001 - 一条线路失败就换下一条
            failures.append(f"{_host_of(line)} {err}")
            continue
        if str(source.get("lastGoodLine") or "") != line:
            source["lastGoodLine"] = line
            store.save()
        return {"value": value, "line": line, "tried": len(failures) + 1}
    raise ValueError(f"全部 {len(lines)} 条线路都失败了：{'；'.join(failures)}")


# ------------------------------------------------------------------ 拉取列表


def _playlist_key(listing: dict) -> str:
    """播放列表的内容指纹：分片地址（或变体地址）就代表了这一段片子"""
    parts = (
        [listing["initSegment"]]
        if listing["initSegment"]
        else listing["segments"]
        if listing["segments"]
        else [v["url"] for v in listing["variants"]]
    )
    return hashlib.md5("|".join(parts).encode("utf-8")).hexdigest()[:16]


async def _fetch_random_clips(source: dict, line: str, headers: dict) -> list[dict]:
    """m3u8 模式：每个线路地址请求一次就返回一段随机短片，所以要连着请求 count 次凑一屏"""
    count = max(1, min(48, int(source.get("count") or 8)))
    seen = _seen_for(source)
    fresh: dict[str, bool] = {}  # 播放列表指纹 → True（这一批该给的）
    stale: dict[str, bool] = {}  # 上一批已经给过的，凑不满时拿来补位
    last_error: Exception | None = None

    for _round in range(3):
        if len(fresh) >= count:
            break
        need = count - len(fresh)
        results = await asyncio.gather(
            *[_probe_playlist(line, headers) for _ in range(need)], return_exceptions=True
        )
        for r in results:
            if isinstance(r, Exception):
                last_error = last_error or r
                continue
            key = _playlist_key(r)
            bucket = stale if key in seen["keys"] else fresh
            # 两个桶各自封顶 count：新的一直能顶掉旧的，最后再按「新的优先」拼一批
            if key not in bucket and len(bucket) < count:
                bucket[key] = True

    # 先给没看过的；池子就那么小时用看过的补齐，至少这一批是凑满的
    picked = [*fresh.keys(), *stale.keys()][:count]
    if not picked:
        raise ValueError(str(last_error) if last_error else "这个地址没返回播放列表")

    _remember_seen(source, picked)
    stamp = _base36(int(time.time() * 1000))
    # 同一个地址每次请求内容都不同，所以 id 不能只用地址，得带上序号
    return [
        {
            "id": f"{line}#{stamp}-{i + 1}",
            "url": line,
            "ext": "m3u8",
            "hls": True,
            "random": True,  # 前端据此知道「下载必定是新的一段」，不做按地址去重
            "poster": "",
            "title": f"第 {i + 1} 条",
        }
        for i in range(len(picked))
    ]


def _base36(value: int) -> str:
    """和 JS `Number.prototype.toString(36)` 同语义（原版用它生成时间戳短码）"""
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    if value <= 0:
        return "0"
    out = ""
    while value:
        value, rem = divmod(value, 36)
        out = digits[rem] + out
    return out


async def _probe_media_alive(media_url: str, headers: dict) -> dict:
    """302 跳出来的直链还活着吗？

    yujn 这类随机源里混着早就被平台删掉的片子：接口照样 302 给你，
    真去取的时候 CDN 才回 404。列表时先探一下，死链当场丢掉换一条。

    平台 CDN 不一定认 HEAD（有的回 405/403），那就退一步发个 Range GET 只取 1 字节。
    顺带把重定向跟到底，返回 `{status, url}`：url 是最终落地的地址，>=400 当作死链。
    """

    async def ask(method: str) -> dict:
        ask_headers = dict(headers)
        if method == "GET":
            ask_headers["Range"] = "bytes=0-1"
        async with httpx.AsyncClient(
            timeout=FETCH_TIMEOUT_MS / 1000, follow_redirects=True
        ) as client:
            res = await client.request(method, media_url, headers=ask_headers)
        return {"status": res.status_code, "url": str(res.url) or media_url}

    try:
        probe = await ask("HEAD")
        if probe["status"] not in (405, 501, 403):
            return probe
    except Exception:  # noqa: BLE001 - HEAD 直接失败（有些 CDN 会掐掉），下面用 GET 再试
        pass
    return await ask("GET")


def media_ext(media_url: str, content_type: str = "") -> str:
    """地址后缀认不出来时按响应类型兜底"""
    from_url = _ext_of(media_url)
    if from_url and from_url in [*VIDEO_EXTS, *IMAGE_EXTS]:
        return from_url
    ct = str(content_type or "")
    if "image" in ct:
        return "jpg"
    return "mp4"


async def _snapshot_response(res: httpx.Response, line: str) -> dict:
    """200 直出：把内容抓下来存成快照，返回一个本地地址（显示/下载都指这一份）"""
    mime = str(res.headers.get("content-type") or "").split(";")[0].strip().lower()
    final_url = str(res.url) or line
    ext = MIME_TO_EXT.get(mime) or media_ext(final_url, mime)
    declared = int(res.headers.get("content-length") or 0)
    # 太大（多半是视频）不入内存：退回原地址，至少内容是对的
    if declared > SNAPSHOT_MAX_BYTES:
        return {"url": final_url, "ext": ext}

    buffer = res.content
    if not buffer:
        raise ValueError("接口返回了空内容")
    if len(buffer) > SNAPSHOT_MAX_BYTES:
        return {"url": final_url, "ext": ext}

    # token 用内容指纹：重复的那份自然合并成同一条
    token = hashlib.md5(buffer).hexdigest()[:24]
    _snapshots[token] = {
        "buffer": buffer,
        "mime": mime or MIME_BY_EXT.get(ext) or "application/octet-stream",
        "at": time.time() * 1000,
    }
    _prune_snapshots()
    return {"url": f"/api/materials/rand/{token}", "ext": ext}


async def _probe_redirect(line: str, headers: dict) -> dict:
    """redirect 模式：请求线路地址，服务器一般 302 跳到一个随机 mp4/jpg。

    不跟随重定向，直接读 302 的 Location 头拿到媒体地址；拿到中间地址后还要再跟一跳到底：
    有的平台会先跳到自己的中转地址，再由中转地址 302 到真正的 CDN；而中转那一跳的 302
    响应不带 CORS 头，前端 `<video crossOrigin="anonymous">` 在 CORS 检查处就被拦掉，
    视频永远不加载。所以这里跟着 `_probe_media_alive` 把跳转走完，直接下发最终 CDN 地址。

    有些接口（如 api.yujn.cn/api/jk.php）不跳转，HTTP 200 直接把内容吐回来。
    这种就得把这一份抓下来单独存，否则每条素材的地址都是接口本身、看起来永远同一张。
    """
    try:
        async with httpx.AsyncClient(timeout=FETCH_TIMEOUT_MS / 1000, follow_redirects=False) as client:
            res = await client.get(line, headers=headers)
    except httpx.TimeoutException as err:
        raise ValueError("请求失败：超时") from err
    except httpx.HTTPError as err:
        raise ValueError(f"请求失败：{err}") from err

    if 300 <= res.status_code < 400:
        media_url = str(res.headers.get("location") or "")
        if media_url:
            try:
                media_url = urljoin(line, media_url)
            except Exception:  # noqa: BLE001
                pass
        if not media_url:
            raise ValueError(f"跳转没带 Location（HTTP {res.status_code}）")
        # 跟到底：把剩下的跳转走完，落地的最终地址才是浏览器能直接播的那条直链
        probe = await _probe_media_alive(media_url, headers)
        if probe["status"] >= 400:
            raise ValueError(f"这份随机素材在远端已失效（HTTP {probe['status']}）")
        final_url = probe["url"] or media_url
        # 指纹按「中转地址」算：抖音把 video_id 放在中转地址里，落到 CDN 的直链上反而没有
        return {
            "url": final_url,
            "ext": media_ext(final_url, res.headers.get("content-type") or ""),
            "key": material_key(media_url),
        }

    if res.status_code >= 400:
        raise ValueError(f"没拿到媒体地址（HTTP {res.status_code}）")
    snap = await _snapshot_response(res, line)
    return {**snap, "key": material_key(snap["url"])}


async def _fetch_random_redirects(source: dict, line: str, headers: dict) -> list[dict]:
    """redirect 模式：连着请求 count 次凑一批（每次都是另一份随机素材），重复的补拉"""
    count = max(1, min(48, int(source.get("count") or 8)))
    seen = _seen_for(source)
    fresh: dict[str, dict] = {}  # 内容指纹 → { url, ext }：这一批该给的
    stale: dict[str, dict] = {}  # 上一批已经给过的，凑不满时拿来补位
    last_error: Exception | None = None

    for _round in range(4):
        if len(fresh) >= count:
            break
        need = count - len(fresh)
        results = await asyncio.gather(
            *[_probe_redirect(line, headers) for _ in range(need)], return_exceptions=True
        )
        for r in results:
            if isinstance(r, Exception):
                last_error = last_error or r
                continue
            key = r.get("key") or r["url"]
            bucket = stale if key in seen["keys"] else fresh
            # 两个桶各自封顶 count：新的一直能顶掉旧的，最后再按「新的优先」拼一批
            if key not in bucket and len(bucket) < count:
                bucket[key] = r

    # 先给没看过的；池子就那么小时用看过的补齐，至少这一批是凑满的
    picked = [*fresh.values(), *stale.values()][:count]
    if not picked:
        raise ValueError(str(last_error) if last_error else "这个地址没返回媒体")

    _remember_seen(source, [*fresh.keys(), *stale.keys()])
    stamp = _base36(int(time.time() * 1000))
    return [
        {
            "id": f"{line}#{stamp}-{i + 1}",
            "url": item["url"],
            "ext": item["ext"],
            "hls": False,
            "random": True,
            "poster": "",
            "title": f"第 {i + 1} 条",
        }
        for i, item in enumerate(picked)
    ]


def _pick_batch(source: dict, items: list[dict]) -> list[dict]:
    """「列表类」分类一次只端出一批（分类的「一次拉几条」）。

    以前是整份清单几百条全塞给界面：翻起来累，而且每次刷新都是一模一样的一批。
    现在随机抽一批，优先抽没看过的；整份都看过了就清掉记忆重新轮。
    """
    count = max(1, min(48, int(source.get("count") or 8)))
    if len(items) <= count:
        return items

    rec = _seen_for(source)
    fresh: list[dict] = []
    stale: list[dict] = []
    for item in items:
        (stale if material_key(item["url"]) in rec["keys"] else fresh).append(item)
    if not fresh:
        _forget_seen(source)  # 轮完了：清空记忆从头开始，否则之后每次都只能端出重复的
        fresh = list(items)
    batch = [*_shuffled(fresh), *_shuffled(stale)][:count]
    _remember_seen(source, [material_key(it["url"]) for it in batch])
    return batch


async def fetch_material_items(source: dict, *, refresh: bool = False) -> dict:
    """拉一个分类的一批素材。返回 `{items, line, tried}`。"""
    # 「抖音采集」不在后端拉：由桌面版素材模块里内嵌的抖音网页负责
    if source.get("mode") == "douyin":
        raise ValueError(
            "「抖音采集」不用在这里刷新：素材模块里会内嵌抖音网页，"
            "在页面里刷到想要的点「＋」加到画布"
        )

    if not refresh:
        hit = _cache.get(source["id"])
        if hit and time.time() * 1000 - hit[0] < CACHE_TTL_MS:
            return {"items": hit[1], "line": hit[2]}

    headers = material_headers(source)
    mode = source.get("mode")

    async def attempt(raw: str) -> list[dict]:
        target = _with_extra_params(_absolute_line(raw), source)
        if mode == "m3u8":
            return await _fetch_random_clips(source, target, headers)
        if mode == "redirect":
            return await _fetch_random_redirects(source, target, headers)
        text = await fetch_text(target, headers, FETCH_TIMEOUT_MS, "请求接口失败")
        items = parse_items(text, source)
        if not items:
            raise ValueError(
                "接口连通了，但没解析出媒体地址：可在「接口管理」里填「列表路径」和"
                "「地址字段」指定，例如 data.list / play_url"
            )
        return _pick_batch(source, items)

    out = await _try_lines(source, attempt)
    _cache[source["id"]] = (time.time() * 1000, out["value"], out["line"])
    return {"items": out["value"], "line": out["line"], "tried": out["tried"]}


# ------------------------------------------------------------------ 图片搜索

IMAGE_SEARCH_SOURCES = [
    {"id": "so360", "name": "360 图片（国漫/游戏/动漫角色最全，中文直接搜）"},
    {"id": "pixiv", "name": "Pixiv 插画（二次元同人，按标签搜，如 初音未来 / 少女）"},
    {"id": "booru", "name": "动漫标签图站（英文 tag 最准，如 1girl white_hair）"},
    {"id": "bing", "name": "必应图片（通用，描述性的话也能搜）"},
]

SEARCH_TIMEOUT_MS = 20000
SEARCH_LIMIT_MAX = 48


def _decode_html_entities(text: str) -> str:
    """必应把图片地址塞在 HTML 属性里做了实体转义（&quot;），取出来前先还原成 JSON"""
    body = str(text)
    body = re.sub(
        r"&#x([0-9a-f]+);",
        lambda m: chr(int(m.group(1), 16)),
        body,
        flags=re.I,
    )
    body = re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))), body)
    return (
        body.replace("&quot;", '"')
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&amp;", "&")
    )


async def _search_fetch(url: str, accept_language: str = "") -> httpx.Response:
    headers = {"User-Agent": UA}
    if accept_language:
        headers["Accept-Language"] = accept_language
    async with httpx.AsyncClient(
        timeout=SEARCH_TIMEOUT_MS / 1000, follow_redirects=True
    ) as client:
        return await client.get(url, headers=headers)


async def _search_pixiv(keyword: str, limit: int) -> list[dict]:
    url = (
        "https://api.lolicon.app/setu/v2"
        f"?num={limit}&r18=0&size=regular&proxy=i.pixiv.re"
        f"&keyword={quote(keyword)}"
    )
    res = await _search_fetch(url)
    if res.status_code >= 400:
        raise ValueError(f"Pixiv 图源返回 HTTP {res.status_code}")
    payload = res.json()
    if isinstance(payload, dict) and payload.get("error"):
        raise ValueError(f"Pixiv 图源报错：{payload['error']}")
    data = (payload or {}).get("data") or [] if isinstance(payload, dict) else []
    return [
        {
            "id": f"pixiv:{d.get('pid')}:{d.get('p')}",
            # 网格里显示 regular（几百 KB，出图快）；「加入参考图」下 original，二创要清晰
            "url": (d.get("urls") or {}).get("original")
            or (d.get("urls") or {}).get("regular")
            or "",
            "thumb": (d.get("urls") or {}).get("regular")
            or (d.get("urls") or {}).get("original")
            or "",
            "width": d.get("width") or 0,
            "height": d.get("height") or 0,
            "title": d.get("title") or "",
            "author": d.get("author") or "",
            "tags": (d.get("tags") or [])[:8],
            "referer": "https://www.pixiv.net/",
        }
        for d in data
        if isinstance(d, dict)
    ]


async def _search_booru(keyword: str, limit: int, page: int) -> list[dict]:
    # 图站用的是英文 tag，空格表示「都要满足」：1girl white_hair 就是白发的单个女孩。
    # 注意这里必须用字面 + 连接（不能把 + 编成 %2B）：safebooru 只认 URL 里的 + 当分隔符，
    # %2B 会被它当成「一个带加号的怪 tag」，直接返回空。
    tags = "+".join(quote(part) for part in str(keyword).strip().split())
    url = (
        "https://safebooru.org/index.php?page=dapi&s=post&q=index&json=1"
        f"&limit={limit}&pid={page}&tags={tags}"
    )
    res = await _search_fetch(url)
    if res.status_code >= 400:
        raise ValueError(f"动漫图站返回 HTTP {res.status_code}")
    text = res.text
    try:
        listing = json.loads(text)
    except json.JSONDecodeError as err:
        raise ValueError("动漫图站返回的不是 JSON，可能被拦了，稍后再试") from err
    if not isinstance(listing, list):
        raise ValueError("动漫图站没给结果")
    out = []
    for p in listing:
        if not isinstance(p, dict):
            continue
        tags_list = str(p.get("tags") or "").split(" ")
        out.append(
            {
                "id": f"booru:{p.get('id')}",
                "url": p.get("file_url") or p.get("sample_url") or "",
                "thumb": p.get("sample_url") or p.get("preview_url") or p.get("file_url") or "",
                "width": p.get("width") or 0,
                "height": p.get("height") or 0,
                "title": " ".join(tags_list[:6]),
                "tags": tags_list[:8],
                "referer": "https://safebooru.org/",
            }
        )
    return out


async def _search_bing(keyword: str, limit: int, page: int) -> list[dict]:
    url = (
        "https://www.bing.com/images/search"
        f"?q={quote(keyword)}&form=HDRSC2&first={page * limit + 1}"
    )
    res = await _search_fetch(url, "zh-CN,zh;q=0.9")
    if res.status_code >= 400:
        raise ValueError(f"必应返回 HTTP {res.status_code}")
    html = res.text

    items: list[dict] = []
    # 每条结果的元数据挂在 <a class="iusc" m="{...}"> 上，里面 murl 是原图、turl 是缩略图
    for hit in re.finditer(r'm="([^"]{0,4000})"', html):
        raw = hit.group(1)
        if "murl" not in raw:
            continue
        try:
            meta = json.loads(_decode_html_entities(raw))
        except json.JSONDecodeError:
            continue
        if not isinstance(meta, dict) or not _is_http_url(meta.get("murl")):
            continue
        thumb = meta.get("turl")
        items.append(
            {
                "id": f"bing:{meta['murl']}",
                "url": meta["murl"],
                "thumb": thumb if _is_http_url(thumb) else meta["murl"],
                "width": 0,
                "height": 0,
                "title": meta.get("t") or "",
                "tags": [],
                "referer": "https://www.bing.com/",
            }
        )
        if len(items) >= limit:
            break
    return items


async def _search_so360(keyword: str, limit: int, page: int) -> list[dict]:
    """360 图片（image.so.com 公开 JSON 接口）：中文关键词直接搜，

    国漫/游戏/动漫角色都全（美杜莎、云曦、雷电将军都能出图）。
    img 字段就是各家图床的原图直链，thumb 是 360 的缩略图；「换一批」= 偏移递增。
    """
    url = (
        "https://image.so.com/j"
        f"?q={quote(keyword)}&sn={page * limit}&pn={limit}&src=srp"
    )
    res = await _search_fetch(url)
    if res.status_code >= 400:
        raise ValueError(f"360 图片返回 HTTP {res.status_code}")
    try:
        payload = res.json()
    except (json.JSONDecodeError, ValueError) as err:
        raise ValueError("360 图片返回的不是 JSON，可能被拦了，稍后再试") from err
    listing = payload.get("list") if isinstance(payload, dict) else None
    listing = listing if isinstance(listing, list) else []
    out = []
    for it in listing:
        if not isinstance(it, dict) or not _is_http_url(it.get("img")):
            continue
        thumb = it.get("thumb")
        out.append(
            {
                "id": f"so360:{it.get('id') or it.get('img')}",
                "url": it["img"],
                "thumb": thumb if _is_http_url(thumb) else it["img"],
                "width": it.get("width") or 0,
                "height": it.get("height") or 0,
                "title": it.get("title") or "",
                "tags": [],
                "referer": "",
            }
        )
    return out[:limit]


async def search_images(*, q, source: str = "pixiv", page=0, limit=24) -> dict:
    """关键词搜图：结果统一成 `{id, url, thumb, width, height, title, tags, referer}`"""
    keyword = str(q or "").strip()
    if not keyword:
        raise ValueError("先输入要搜的关键词")

    size = max(1, min(SEARCH_LIMIT_MAX, int(limit or 24)))
    offset = max(0, int(page or 0))

    if source == "booru":
        items = await _search_booru(keyword, size, offset)
    elif source == "bing":
        items = await _search_bing(keyword, size, offset)
    elif source == "so360":
        items = await _search_so360(keyword, size, offset)
    else:
        items = await _search_pixiv(keyword, size)

    # 只留直链：搜索引擎偶尔会夹带网页地址进来
    clean = [it for it in items if _is_http_url(it["url"]) and _is_http_url(it["thumb"])]
    if not clean:
        tips = {
            "so360": "换个更具体的角色名试试，如「美杜莎 斗破苍穹」「云曦 完美世界」「雷电将军 原神」",
            "pixiv": (
                "Pixiv 是按标签找图的，换成更短的标签再试（如「少女」「初音未来」）；"
                "描述性的话切「360 图片」或「必应图片」"
            ),
            "booru": "这个源只认英文 tag，试试 1girl white_hair 这种写法；中文关键词切「360 图片」或「Pixiv」",
            "bing": "换个关键词再试",
        }
        raise ValueError(f"没搜到 —— {tips.get(source, '换个关键词再试')}")
    return {"source": source, "q": keyword, "items": clean}


async def random_images(*, page=0, limit=16) -> dict:
    """随机二次元人物：lolicon 公开接口不传 keyword 就是全图库随机。

    返回一批带作品名/画师/标签的插画（r18=0 只收全年龄），前端墙里点一张即可加入参考图。
    lolicon 免费接口 num 上限 20；随机没有「翻页」概念，前端「换一批」= 再随机一批。
    """
    size = max(1, min(20, int(limit or 16)))
    items = await _search_pixiv("", size)
    clean = [it for it in items if _is_http_url(it["url"]) and _is_http_url(it["thumb"])]
    if not clean:
        raise ValueError("随机图暂时没取到，点「换一批」再试试")
    return {"items": clean}


# ------------------------------------------------------------------ pixiv 排行榜

IMAGE_RANK_MODES = [
    {"id": "day", "name": "日榜"},
    {"id": "week", "name": "周榜"},
    {"id": "month", "name": "月榜"},
    {"id": "male", "name": "男性向榜"},
    {"id": "female", "name": "女性向榜"},
]

PIXIVIC_RANK_API = "https://pix.ipv4.host/ranks"


def _to_reachable_image(url) -> str:
    """i.pximg.net 连不上：换成同路径的公开图片代理 i.pixiv.re"""
    return re.sub(r"^https?://i\.pximg\.net/", "https://i.pixiv.re/", str(url or "").strip(), flags=re.I)


def _rank_dates() -> list[str]:
    """榜单日期候选：昨天起往前 4 天"""
    today = time.time() * 1000
    return [time.strftime("%Y-%m-%d", time.gmtime((today - i * 86400000) / 1000)) for i in range(1, 5)]


#: mode → {date, at}
_rank_date_cache: dict[str, dict[str, Any]] = {}
RANK_DATE_TTL_MS = 30 * 60 * 1000


async def _rank_request(mode: str, date: str, page: int, size: int) -> list:
    url = (
        f"{PIXIVIC_RANK_API}?page={page}&pageSize={size}"
        f"&date={date}&mode={quote(mode)}"
    )
    res = await _search_fetch(url)
    if res.status_code >= 400:
        raise ValueError(f"排行榜接口返回 HTTP {res.status_code}")
    try:
        payload = res.json()
    except (json.JSONDecodeError, ValueError) as err:
        raise ValueError("排行榜接口返回的不是 JSON，可能被拦了，稍后再试") from err
    if isinstance(payload, dict) and payload.get("error"):
        raise ValueError(f"排行榜接口报错：{payload['error']}")
    data = payload.get("data") if isinstance(payload, dict) else None
    return data if isinstance(data, list) else []


async def _resolve_rank_date(mode: str) -> str:
    hit = _rank_date_cache.get(mode)
    if hit and time.time() * 1000 - hit["at"] < RANK_DATE_TTL_MS:
        return hit["date"]
    for date in _rank_dates():
        if await _rank_request(mode, date, 1, 5):
            _rank_date_cache[mode] = {"date": date, "at": time.time() * 1000}
            return date
    raise ValueError("排行榜暂时取不到数据（站点可能在维护），稍后再试")


def _map_rank_item(item: dict) -> dict | None:
    """榜单条目 → 统一的搜图结果结构；广告位（type=ad_image）和 R-18 直接丢掉"""
    if item.get("type") == "ad_image":
        return None  # 接口会在榜单里插广告位，得先挑出去
    images = item.get("imageUrls") if isinstance(item.get("imageUrls"), list) else []
    first = images[0] if images and isinstance(images[0], dict) else {}
    url = _to_reachable_image(
        first.get("original") or first.get("large") or first.get("medium") or ""
    )
    thumb = _to_reachable_image(
        first.get("medium")
        or first.get("large")
        or first.get("squareMedium")
        or first.get("original")
        or ""
    )
    if not _is_http_url(url) or not _is_http_url(thumb):
        return None
    try:
        if int(item.get("xrestrict") or 0) > 0:
            return None  # 和搜索源一样只收全年龄
    except (TypeError, ValueError):
        pass
    tags = [
        (t.get("translatedName") or t.get("name"))
        for t in (item.get("tags") or [])
        if isinstance(t, dict)
    ]
    return {
        "id": f"pixivic:{item.get('id')}",
        "url": url,
        "thumb": thumb,
        "width": item.get("width") or 0,
        "height": item.get("height") or 0,
        "title": item.get("title") or "",
        "author": (item.get("artistPreView") or {}).get("name") or "",
        "tags": [t for t in tags if t][:8],
        # 图片走 i.pixiv.re 代理，不用特意带 Referer
        "referer": "",
    }


async def fetch_ranking(*, mode: str = "day", page=0, limit=24) -> dict:
    """pixiv 排行榜：`{mode, page, limit}` → `{mode, date, items}`"""
    rank_mode = mode if any(m["id"] == mode for m in IMAGE_RANK_MODES) else "day"
    size = max(1, min(SEARCH_LIMIT_MAX, int(limit or 24)))
    page_no = max(1, int(page or 0) + 1)
    date = await _resolve_rank_date(rank_mode)
    raw = await _rank_request(rank_mode, date, page_no, size)
    items = [it for it in (_map_rank_item(x) for x in raw if isinstance(x, dict)) if it]
    return {"mode": rank_mode, "date": date, "items": items}


# ------------------------------------------------------------------ 下载落盘


def _find_downloaded(file_hash: str) -> str:
    """同一个地址已经下过就直接复用，不重复占磁盘（文件名 = md5(url)）"""
    directory = get_paths().materials
    try:
        entries = [p.name for p in directory.iterdir()]
    except OSError:
        return ""
    for name in entries:
        if name.startswith(f"{file_hash}."):
            return name
    return ""


def _describe(file_name: str) -> dict:
    local_path = get_paths().materials / file_name
    ext = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""
    return {
        "fileName": file_name,
        "localPath": str(local_path),
        "localUrl": f"/files/materials/{file_name}",
        "mimeType": MIME_BY_EXT.get(ext) or "application/octet-stream",
        "size": local_path.stat().st_size,
        "ext": ext,
    }


async def _write_hls(playlist_url: str, headers: dict, playlist_text: str, file_hash: str) -> dict:
    """m3u8 下载的落盘尾巴：把分片合并结果写成文件"""
    merged = await _merge_hls(playlist_url, headers, playlist_text)
    ext = merged["ext"]
    file_name = f"{file_hash}.{ext}"
    local_path = get_paths().materials / file_name
    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.write_bytes(merged["buffer"])
    return {
        "fileName": file_name,
        "localPath": str(local_path),
        "localUrl": f"/files/materials/{file_name}",
        "mimeType": MIME_BY_EXT.get(ext) or "video/mp4",
        "size": len(merged["buffer"]),
        "ext": ext,
    }


async def download_material(
    raw_url,
    *,
    kind: str = "video",
    referer: str = "",
    headers: dict | None = None,
    unique: bool = False,
) -> dict:
    """把远端素材下载到本地 materials 目录。

    m3u8 走「拉播放列表 → 逐片下载 → 依序合并」；其余按直链原样保存。
    前提里说好了「接口本来就给无水印原片」，所以这里原样保存，不做任何处理。

    `unique`：给「每次请求都是另一段随机短片」的线路用 —— 文件名带上时间戳，
    否则同一线路下载多次会互相覆盖，只剩最后一段。
    """
    # 「200 直出」接口的素材是本地快照地址（/api/materials/rand/xxx），补成本机绝对地址再取
    raw = str(raw_url or "").strip()
    url = f"{_local_base}{raw}" if raw.startswith("/") and _local_base else raw
    if not _is_http_url(url):
        raise ValueError("素材地址不是合法的 http(s) 链接")

    req_headers = {"User-Agent": UA, **(headers or {})}
    if referer:
        req_headers["Referer"] = referer

    seed = f"{url}#{_base36(int(time.time() * 1000))}" if unique else url
    file_hash = hashlib.md5(seed.encode("utf-8")).hexdigest()

    # HLS：直下 m3u8 只会拿到几 KB 文本，必须按分片合并成可播放的文件
    if _is_m3u8_url(url):
        return await _write_hls(
            url, req_headers, await fetch_text(url, req_headers, FETCH_TIMEOUT_MS, "拉取播放列表失败"), file_hash
        )

    if not unique:
        existed = _find_downloaded(file_hash)
        if existed:
            return _describe(existed)

    try:
        async with httpx.AsyncClient(
            timeout=DOWNLOAD_TIMEOUT_MS / 1000, follow_redirects=True
        ) as client:
            res = await client.get(url, headers=req_headers)
    except httpx.TimeoutException as err:
        raise ValueError("下载失败：超时（60 秒没下完）") from err
    except httpx.HTTPError as err:
        raise ValueError(f"下载失败：{err}") from err
    if res.status_code >= 400:
        raise ValueError(f"下载失败：远端返回 HTTP {res.status_code}")

    mime_type = str(res.headers.get("content-type") or "").split(";")[0].strip().lower()

    # 地址里没 .m3u8 后缀、但返回的就是播放列表（很多中转接口长这样）：也得走合并
    if M3U8_MIME.search(mime_type):
        return await _write_hls(str(res.url) or url, req_headers, res.text, file_hash)

    buffer = res.content
    if not buffer:
        raise ValueError("下载失败：拿到的文件是空的")

    # 地址里没扩展名时（很多 CDN 是纯 ID 路径）用响应类型兜底
    ext = _ext_of(url) or MIME_TO_EXT.get(mime_type) or ("jpg" if kind == "image" else "mp4")
    file_name = f"{file_hash}.{ext}"
    local_path = get_paths().materials / file_name
    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.write_bytes(buffer)

    return {
        "fileName": file_name,
        "localPath": str(local_path),
        "localUrl": f"/files/materials/{file_name}",
        "mimeType": mime_type or MIME_BY_EXT.get(ext) or "application/octet-stream",
        "size": len(buffer),
        "ext": ext,
    }


def import_local_material(raw_path, kind: str = "video") -> dict:
    """登记一个「主进程已经下好的本地文件」，并**收进持久的 materials 目录**。

    抖音那条链路必须带登录态下载，而登录态只有桌面壳拿得到（HTTP 服务直接下会 403），
    所以文件先在桌面壳的临时目录落盘（见 `app/desktop/douyin.py`），这里再收进来。

    为什么要搬这一步（而不是直接引用临时文件）：`douyin-tmp` 是临时目录，会被清理；
    如果素材记录直接指向它，临时文件一没了素材就废了。而且投送出去的素材现在都是
    「共享引用」（画布/视频模块指向同一份文件），一旦引用归零就有权删除 ——
    删掉临时目录里的文件等于把源头也删了。收进 materials/ 之后，这份文件才是
    「素材的正式副本」，由它自己去承担引用计数。

    用内容的 md5 命名：同一个文件重复登记不会产生第二份（天然去重）。
    """
    root = (DATA_DIR / "douyin-tmp").resolve()
    target = Path(str(raw_path or "")).resolve()
    if target != root and root not in target.parents:
        raise ValueError("本地文件不在允许的临时目录里，已拒绝")
    stat = target.stat()  # 文件不存在会直接抛错
    if not target.is_file():
        raise ValueError("下载失败：本地文件不存在")
    if stat.st_size == 0:
        raise ValueError("下载失败：拿到的文件是空的")
    ext = target.suffix.lower() or ("jpg" if kind == "image" else ".mp4")

    # 收进持久目录：按内容 hash 命名，同内容只存一份
    buffer = target.read_bytes()
    file_hash = hashlib.md5(buffer).hexdigest()
    file_name = f"{file_hash}{ext}"
    local_path = get_paths().materials / file_name
    if not local_path.exists():
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(buffer)

    return {
        "fileName": file_name,
        "localPath": str(local_path),
        "localUrl": f"/files/materials/{file_name}",
        "mimeType": MIME_BY_EXT.get(ext.lstrip(".")) or "application/octet-stream",
        "size": stat.st_size,
        "ext": ext,
    }
