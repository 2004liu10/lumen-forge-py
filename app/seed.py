"""内置模型与素材源的首次播种 —— 对应 `server/store.js:769-1059` 的 `seed()`。

## 与原版的差异（重要）

原版 `seed()` 里有 **30 多个一次性布尔标记**（`imageModelsSeeded`、`yujnSeeded`、
`trimmedYujnSources`、`localListsRemoved` ……）。它们是这个产品迭代两年留下的历史包袱：
每一版新加一个内置模型，就加一个标记，保证「只补一次、用户删掉后不再复活」。

对新建的 Python 版来说，**这些标记全都不需要**：
新库第一次启动时，直接把「最终该有的状态」一次写进去即可 —— 效果等价于一个
把所有历史迁移都跑完的老库。这就是本模块做的事。

唯一需要保留的语义是：
- 内置模型「只种一次」→ 用 `settings.seeded` 记录一个版本号，用户删了不再复活；
- 素材分类的默认线路池同上。

## 播种顺序

按原版最终状态写：
1. 4 个空画布
2. 9 个内置图片模型（ark / zhipu / jimeng / wanxiang 图生图 / wanxiang 文生图 /
   hunyuan / minimax / cloudflare×3 / doubao）
3. 2 个内置视频模型（zhipu / doubao）
4. 素材分类：抖音采集（置顶）+ 随机短视频 + 随机小姐姐 + 快手随机 + 随机视频·备用
   + yujn 那 5 个 redirect 分类
"""

from __future__ import annotations

import logging
from typing import Any

from .models import new_canvas, new_image_model, new_material_source, new_video_model
from .providers.imagegen import (
    CLOUDFLARE_BASE,
    HUNYUAN_BASE,
    JIMENG_BASE,
    MINIMAX_BASE,
    WANXIANG_BASE,
    ZHIPU_BASE,
)
from .store import store

log = logging.getLogger(__name__)

#: 播种版本号。改了内置清单就 +1，老库会补种缺的那些。
SEED_VERSION = 1

# ============================================================ 内置图片模型

BUILTIN_IMAGE_MODELS: list[dict[str, Any]] = [
    {
        "name": "豆包 Seedream 4.0（图生图专用）",
        "protocol": "ark",
        "baseUrl": "https://ark.cn-beijing.volces.com/api/v3",
        "model": "doubao-seedream-4-0-250828",
        "size": "2K",
        "noWatermark": True,
        "supportsImageInput": True,
    },
    {
        "name": "智谱 CogView-3-Flash（免费）",
        "protocol": "zhipu",
        "baseUrl": ZHIPU_BASE,
        "model": "cogview-3-flash",
        "size": "1024x1024",
        "noWatermark": True,
    },
    {
        "name": "即梦 AI（文生图 / 图生图）",
        "protocol": "jimeng",
        "baseUrl": JIMENG_BASE,
        "model": "jimeng_t2i_v40",
        "size": "2048x2048",
        "noWatermark": True,
        "supportsImageInput": True,
        "akSecret": "",
    },
    {
        "name": "通义万相·图生图（免费500张）",
        "protocol": "wanxiang",
        "baseUrl": WANXIANG_BASE,
        "model": "wanx2.1-imageedit",
        "size": "",
        "noWatermark": True,
        "supportsImageInput": True,
    },
    {
        "name": "通义万相·文生图（免费额度）",
        "protocol": "wanxiang",
        "baseUrl": WANXIANG_BASE,
        "model": "wan2.2-t2i-flash",
        "size": "",
        "noWatermark": True,
    },
    {
        "name": "腾讯混元生图 3.0（TokenHub）",
        "protocol": "hunyuan",
        "baseUrl": HUNYUAN_BASE,
        "model": "hy-image-v3",
        "size": "1024:1024",
        "noWatermark": True,
        "supportsImageInput": True,
    },
    {
        "name": "MiniMax 海螺 image-01",
        "protocol": "minimax",
        "baseUrl": MINIMAX_BASE,
        "model": "image-01",
        "size": "1:1",
        "noWatermark": True,
        "supportsImageInput": True,
    },
    {
        "name": "Cloudflare FLUX.2 klein-4b（免费）",
        "protocol": "cloudflare",
        "baseUrl": CLOUDFLARE_BASE,
        "accountId": "",
        "model": "@cf/black-forest-labs/flux-2-klein-4b",
        "size": "1024x1024",
        "noWatermark": True,
        "supportsImageInput": True,
    },
    {
        "name": "Cloudflare FLUX.2 dev（免费档 · 画质最好）",
        "protocol": "cloudflare",
        "baseUrl": CLOUDFLARE_BASE,
        "accountId": "",
        "model": "@cf/black-forest-labs/flux-2-dev",
        "size": "1024x1024",
        "noWatermark": True,
        "supportsImageInput": True,
    },
    {
        "name": "Cloudflare FLUX.2 klein-9b（免费档 · 居中）",
        "protocol": "cloudflare",
        "baseUrl": CLOUDFLARE_BASE,
        "accountId": "",
        "model": "@cf/black-forest-labs/flux-2-klein-9b",
        "size": "1024x1024",
        "noWatermark": True,
        "supportsImageInput": True,
    },
    {
        # 本机豆包桥：不需要 API Key，驱动本机装着的豆包桌面客户端出图
        "name": "豆包客户端（本机免 Key）",
        "protocol": "doubao",
        "baseUrl": "http://127.0.0.1:9555",
        "model": "",
        "size": "",
        "noWatermark": False,  # 客户端出图带水印，没有可关的接口
        "supportsImageInput": True,
    },
]

# ============================================================ 内置视频模型

BUILTIN_VIDEO_MODELS: list[dict[str, Any]] = [
    {
        "name": "智谱 CogVideoX-Flash（免费）",
        "provider": "zhipu",
        "baseUrl": "https://open.bigmodel.cn",
        "model": "cogvideox-flash",
        # cogvideox-flash 只有 5 / 10 两档，横屏是这一版的主用画幅
        "defaultDuration": "10",
        # 智谱只认「宽x高」
        "defaultResolution": "1920x1080",
        "defaultRatio": "",
    },
    {
        "name": "本地豆包（客户端桥）",
        "provider": "doubao",
        # 这里是 CDP 调试端口，不是 HTTP 接口地址
        "baseUrl": "http://127.0.0.1:9555",
        "model": "doubao-local-video",
        "defaultDuration": "10",
        "defaultResolution": "",
        "defaultRatio": "16:9",
    },
]

# ============================================================ 素材分类

#: yujn 那批 redirect 分类：请求后 302 跳到随机 mp4/jpg
_YUJN_BASE = "http://api.yujn.cn/api"

#: 内置素材分类的最终状态（顺序即界面顺序，抖音采集在最前）
BUILTIN_MATERIAL_SOURCES: list[dict[str, Any]] = [
    {
        # 不在后端拉：桌面版素材模块里内嵌真抖音网页，用户自己刷、点「＋」投送进画布
        "name": "抖音采集",
        "kind": "video",
        "mode": "douyin",
        # 抖音 CDN 认 Referer，带上站点首页才不会被判盗链
        "headers": '{"Referer":"https://www.douyin.com/"}',
    },
    {
        # 接口从目标 APK 逆向得到：GET https://p.txqq.pro/api/girls?limit=20
        # 返回 { code: 0, list: [{ video_url, ... }] }，video_url 是快手 CDN 直链
        "name": "随机短视频",
        "kind": "video",
        "mode": "api",
        "lines": ["https://p.txqq.pro/api/girls?limit=20"],
        "itemsPath": "list",
        "urlField": "video_url",
        "count": 20,
    },
    {
        # api.yujn.cn/api/zzxjj.php?type=video 302 跳一段随机 mp4
        "name": "随机小姐姐",
        "kind": "video",
        "mode": "redirect",
        "lines": ["https://api.yujn.cn/api/zzxjj.php?type=video"],
    },
    {
        # openapi.dwo.cc/api/ksvideo 302 跳一段随机 mp4
        "name": "快手随机",
        "kind": "video",
        "mode": "redirect",
        "lines": ["https://openapi.dwo.cc/api/ksvideo"],
    },
    {
        # 返回 JSON 单个视频 {data:{video:{url}}}，一次刷新一条
        "name": "随机视频·备用",
        "kind": "video",
        "mode": "api",
        "lines": ["https://openapi.dwo.cc/api/4qb_sp?type=json"],
        "itemsPath": "data.video",
        "urlField": "url",
        "count": 1,
    },
    {"name": "热舞", "kind": "video", "mode": "redirect", "lines": [f"{_YUJN_BASE}/rewu.php"]},
    {"name": "慢摇", "kind": "video", "mode": "redirect", "lines": [f"{_YUJN_BASE}/manyao.php"]},
    {"name": "JK", "kind": "image", "mode": "redirect", "lines": [f"{_YUJN_BASE}/jk.php"]},
    {"name": "黑丝", "kind": "image", "mode": "redirect", "lines": [f"{_YUJN_BASE}/heisi.php"]},
    {"name": "白丝", "kind": "image", "mode": "redirect", "lines": [f"{_YUJN_BASE}/baisi.php"]},
]


# ============================================================ 播种


def seed() -> bool:
    """首次启动播种。返回是否改动过（改了会 flush）。"""
    changed = False

    # 1. 空画布：开箱即可多开
    if not store.canvases:
        for i in range(1, 5):
            store.canvases.append(new_canvas(f"画布 {i}"))
        changed = True

    settings = dict(store.settings or {})
    seeded = int(settings.get("seeded") or 0)

    # 2. 内置图片模型：按 (protocol, model) 判重，缺哪个补哪个。
    #    用 seeded 版本号保证「用户删掉之后不再复活」。
    if seeded < SEED_VERSION:
        existing_img = {(m.get("protocol"), m.get("model")) for m in store.image_models}
        # Cloudflare 三个型号共享凭据：先找已有凭据的作为 donor
        cf_donor = next(
            (m for m in store.image_models if m.get("protocol") == "cloudflare" and m.get("accountId")),
            next((m for m in store.image_models if m.get("protocol") == "cloudflare"), None),
        )
        for builtin in BUILTIN_IMAGE_MODELS:
            key = (builtin["protocol"], builtin.get("model") or "")
            if key in existing_img:
                continue
            made = new_image_model(builtin)
            # Cloudflare 新型号继承已有凭据，省得用户重填 Account ID 和 Token
            if builtin["protocol"] == "cloudflare" and cf_donor:
                made["accountId"] = cf_donor.get("accountId") or ""
                made["apiKey"] = cf_donor.get("apiKey") or ""
            store.image_models.append(made)
            changed = True

        # 3. 内置视频模型：按 (provider, model) 判重
        existing_vid = {(m.get("provider"), m.get("model")) for m in store.video_models}
        for builtin in BUILTIN_VIDEO_MODELS:
            key = (builtin["provider"], builtin.get("model") or "")
            if key in existing_vid:
                continue
            store.video_models.append(new_video_model(builtin))
            changed = True

        # 4. 素材分类：按名字判重，缺哪个补哪个
        existing_src = {s.get("name") for s in store.material_sources}
        for builtin in BUILTIN_MATERIAL_SOURCES:
            if builtin["name"] in existing_src:
                continue
            store.material_sources.append(new_material_source(builtin))
            changed = True

        settings["seeded"] = SEED_VERSION
        store.replace("settings", settings)
        changed = True

    if changed:
        for table in ("canvases", "image_models", "video_models", "material_sources", "settings"):
            store.mark_dirty(table)
        store.flush()
        log.info("[seed] 首次播种完成：图片模型 %s 个 / 视频模型 %s 个 / 素材分类 %s 个",
                 len(store.image_models), len(store.video_models), len(store.material_sources))

    return changed
