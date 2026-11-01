"""记录工厂函数：把「一条新记录长什么样」集中在这里。

对应 Node 版 `server/store.js:1061-1349` 的 `newCanvas` / `newAccount` /
`newImageModel` … 一族函数。原版把这些默认值写在工厂函数里，路由层只负责
拿 `req.body` 覆盖 —— 这样「界面上没填的字段」永远有默认值，前端不必逐个判断
`undefined`。这里照搬同样的策略。

**字段名一律保持驼峰**（`createdAt`、`modelId`、`supportsImageInput`…），
原因见 `db.py` 顶部说明：payload 列存的是原样的 JS 对象，前端读到的
JSON 必须与 Node 版逐字段一致，否则 18.7k 行前端代码要跟着改。
只有 **SQLite 表名**是下划线风格。
"""

from __future__ import annotations

import re
from typing import Any

from .ids import next_id, now_ms

# ---------------------------------------------------------------- 画布（RunningHub）


def new_canvas(name: str) -> dict[str, Any]:
    """一张 RunningHub 画布（= 一个 AI 应用或 ComfyUI 工作流的调用配置）。

    对应 store.js:1061 `newCanvas`。
    """
    return {
        "id": next_id("cv"),
        "name": name,
        "mode": "app",  # app = AI 应用(webappId) | workflow = ComfyUI 工作流(workflowId)
        "baseUrl": "https://www.runninghub.cn",
        "appId": "",
        "appName": "",
        "fields": [],
        "values": {},
        "assets": {},
        "accountId": None,  # None = 由调度器自动分配
        "autoRetry": True,
        "maxAttempts": 2,
        "removeWatermark": True,  # 提交前自动把水印相关的节点参数置为「关闭」
        "polishAppId": "",  # 去水印应用的 webappId
        "webFallback": True,  # 接口被判「余额不足」时改用网页端跑
        "webUrl": "",
        "instanceType": "",  # 应用要求的实例类型（plus / ultra）
        "createdAt": now_ms(),
    }


# -------------------------------------------------------------------- 账号


def new_account(name: str, api_key: str) -> dict[str, Any]:
    """一个 RunningHub 账号。对应 store.js:1084 `newAccount`。"""
    return {
        "id": next_id("acc"),
        "name": name,
        "apiKey": api_key,
        "site": "cn",  # cn = 国内站 runninghub.cn | ai = 国际站 runninghub.ai
        "concurrency": 1,
        "enabled": True,
        "remark": "",
        "dailyLimit": 1,  # 免费额度：每个 24h 窗口期可用次数，0 = 不限制
        "quotaUsed": 0,
        "quotaWindowStart": None,
        "stats": {"total": 0, "success": 0, "failed": 0},
        "loginName": "",
        "loginPass": "",
        "loginAt": None,
        "claimAt": None,
        "claimGain": None,
        "claimNote": "",
        "createdAt": now_ms(),
    }


# ---------------------------------------------------------------- 生图模型


def new_image_model(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """一个生图模型配置。对应 store.js:1107 `newImageModel`。

    `protocol` 决定走哪套 HTTP 协议，`providers/imagegen.py` 按它分发。
    """
    base = {
        "id": next_id("im"),
        "name": "图片模型",
        # ark = 火山方舟豆包 | openai = OpenAI 兼容 | zhipu = 智谱 CogView
        # jimeng = 即梦（AK/SK 签名）| wanxiang = 通义万相（DashScope）
        # hunyuan = 腾讯混元（TokenHub）| minimax = MiniMax 海螺
        # cloudflare = Cloudflare Workers AI（FLUX.2）| doubao = 本机豆包客户端桥
        "protocol": "ark",
        "baseUrl": "https://ark.cn-beijing.volces.com/api/v3",
        "apiKey": "",  # jimeng 下存 AccessKeyID；cloudflare 下存 API Token
        "akSecret": "",  # 仅 jimeng：AccessKeySecret
        "accountId": "",  # 仅 cloudflare：Cloudflare 账号 ID
        "model": "",
        "size": "2K",
        "noWatermark": True,  # ark 下发 watermark=false，产出天然无水印
        "supportsImageInput": False,
        "extraParams": "",  # JSON 字符串，原样并入请求体
        "enabled": True,
        "createdAt": now_ms(),
    }
    if overrides:
        base.update(overrides)
    return base


def new_image_ref(
    *,
    original_name: str,
    file_name: str,
    local_path: str,
    local_url: str,
    mime_type: str,
    size: int,
    source: str | None = None,
    url: str | None = None,
) -> dict[str, Any]:
    """图生图参考图登记。对应 store.js:1129 `newImageRef`。

    `url` 非空 = 外网链接图（即梦这类只收公网 URL 的协议用），此时无本地文件。
    """
    return {
        "id": next_id("ref"),
        "originalName": original_name,
        "fileName": file_name,
        "localPath": local_path,
        "localUrl": local_url,
        "mimeType": mime_type,
        "size": size,
        "url": url or "",
        "source": source or "upload",  # upload | generated | link
        "createdAt": now_ms(),
    }


def new_image_task(
    *,
    model_id: str,
    model_name: str,
    prompt: str,
    size: str,
    count: int,
    refs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """一条生图任务。对应 store.js:1144 `newImageTask`。

    `refs` 是**参考图快照**：带路径，生成时直接读文件；图库被清掉也不影响这条记录。
    """
    return {
        "id": next_id("img"),
        "modelId": model_id,
        "modelName": model_name,
        "prompt": prompt,
        "size": size,
        "count": count,
        "refs": refs or [],
        "status": "queued",  # queued | running | success | failed
        "images": [],
        "error": "",
        "progress": "",  # 长任务（本地豆包桥）的进度文案
        "attempts": 0,
        "cancelRequested": False,
        "createdAt": now_ms(),
        "startedAt": None,
        "finishedAt": None,
    }


# ---------------------------------------------------------------- 生视频


def new_video_model(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """一个生视频模型配置。对应 store.js:1168 `newVideoModel`。"""
    base = {
        "id": next_id("vm"),
        "name": "视频模型",
        "provider": "zhipu",  # zhipu = 智谱 BigModel | ark = 火山方舟 | dashscope = 阿里云百炼
        "baseUrl": "https://open.bigmodel.cn",
        "apiKey": "",
        "model": "",
        "defaultDuration": "",
        "defaultResolution": "",
        "defaultRatio": "",
        "extraParams": "",
        "enabled": True,
        "createdAt": now_ms(),
    }
    if overrides:
        base.update(overrides)
    return base


def new_video_input(
    *,
    file_name: str,
    original_name: str | None = None,
    local_path: str,
    local_url: str,
    mime_type: str,
    size: int,
    source: str | None = None,
    source_task_id: str | None = None,
    source_image_id: str | None = None,
) -> dict[str, Any]:
    """视频模块的输入图（首帧 / 尾帧）。对应 store.js:1187 `newVideoInput`。"""
    return {
        "id": next_id("vin"),
        "fileName": file_name,
        "originalName": original_name or file_name,
        "localPath": local_path,
        "localUrl": local_url,
        "mimeType": mime_type,
        "size": size,
        "source": source or "upload",  # upload | fromImage
        "sourceTaskId": source_task_id or "",
        "sourceImageId": source_image_id or "",
        "createdAt": now_ms(),
    }


def new_video_task(
    *,
    model: dict[str, Any],
    prompt: str,
    params: dict[str, Any] | None = None,
    input: dict[str, Any] | None = None,
    end_input: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """一条生视频任务。对应 store.js:1203 `newVideoTask`。

    状态机：queued → submitting → polling → downloading → success | failed | cancelled
    """
    return {
        "id": next_id("vid"),
        "modelId": model.get("id"),
        "modelName": model.get("name"),
        "provider": model.get("provider"),
        "prompt": prompt,
        "params": params or {},  # { duration, resolution, ratio, seed }
        "input": input or None,
        "endInput": end_input or None,  # 尾帧
        "status": "queued",
        "remoteTaskId": "",
        "remoteStatus": "",
        "nextPollAt": 0,
        "video": None,
        "lastFrame": None,
        "error": "",
        "attempts": 0,
        "cancelRequested": False,
        "createdAt": now_ms(),
        "startedAt": None,
        "finishedAt": None,
    }


# ---------------------------------------------------------------- 分镜剧本


def new_storyboard(
    *,
    title: str | None = None,
    style: str | None = None,
    characters: list[dict[str, Any]] | None = None,
    shots: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """一份分镜剧本。对应 store.js:1245 `newStoryboard`。

    角色带 refId（定妆照），之后每个镜头出图都拿它当图生图参考，保证跨镜头长相一致。
    镜头只记「挂了哪条任务」，不存结果快照 —— 任务会被重试/裁剪，存快照会对不上。
    """
    chars = []
    for c in characters or []:
        chars.append(
            {
                "name": str((c or {}).get("name") or "").strip(),
                "desc": str((c or {}).get("desc") or "").strip(),
                "refId": (c or {}).get("refId") or "",
                "refUrl": (c or {}).get("refUrl") or "",
                "refTaskId": (c or {}).get("refTaskId") or "",
                "fromPhoto": bool((c or {}).get("fromPhoto")),
            }
        )
    out_shots = []
    for i, s in enumerate(shots or []):
        s = s or {}
        names = s.get("characters")
        out_shots.append(
            {
                "index": i + 1,
                "scene": str(s.get("scene") or "").strip(),
                "dialogue": str(s.get("dialogue") or "").strip(),
                "characters": [str(n or "").strip() for n in names] if isinstance(names, list) else [],
                "imageTaskId": "",
                "videoTaskId": "",
                "note": "",
            }
        )
    return {
        "id": next_id("sb"),
        "title": str(title or "未命名小剧场").strip(),
        "style": str(style or "真人 cos 风格，写实摄影质感").strip(),
        "characters": chars,
        "shots": out_shots,
        "status": "draft",  # draft | casting | drawing | done
        "error": "",
        "createdAt": now_ms(),
    }


# ------------------------------------------------------------------ 话题库


def new_topic(
    *, name: str | None = None, source: str | None = None, source_desc: str | None = None
) -> dict[str, Any]:
    """一条话题。对应 store.js:1280 `newTopic`。

    `name` 不带 #（界面上统一拼），去掉所有空白，最长 30 字。
    """
    clean = re.sub(r"^#+", "", str(name or ""))
    clean = re.sub(r"\s+", "", clean).strip()[:30]
    return {
        "id": next_id("tp"),
        "name": clean,
        "source": str(source or "douyin").strip(),  # douyin | manual | ai
        "sourceDesc": str(source_desc or "").strip()[:80],
        "usedCount": 0,
        "lastUsedAt": 0,
        "createdAt": now_ms(),
    }


# ---------------------------------------------------------------- 素材分类


def new_material_source(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """一个素材分类（= 一条独立接口）。对应 store.js:1299 `newMaterialSource`。"""
    base = {
        "id": next_id("ms"),
        "name": "素材分类",
        "kind": "video",  # video | image
        # api = 普通接口返回列表 | m3u8 = 随机短片线路池
        # redirect = 302 跳随机直链 | douyin = 桌面版内置浏览器采集
        "mode": "api",
        "lines": [],  # 线路池，失败自动换下一条
        "clip": "",  # 仅 m3u8：本分类短片编号
        "count": 8,
        "headers": "",  # JSON 字符串，并入请求头
        "apiKey": "",
        "apiKeyHeader": "Authorization",
        "apiKeyPrefix": "Bearer ",
        "itemsPath": "",  # 列表在返回 JSON 里的点路径
        "urlField": "",  # 每项里放媒体地址的字段名
        "extraParams": "",  # JSON 字符串，并入 query string
        "enabled": True,
        "createdAt": now_ms(),
    }
    if overrides:
        base.update(overrides)
    return base


def normalize_material_lines(lines: Any) -> list[str]:
    """线路池归一化：兼容老数据里的字符串 / `{url}` 混合写法，丢掉空行。

    对应 store.js:1326 `normalizeMaterialLines`。
    """
    if not isinstance(lines, list):
        return []
    out: list[str] = []
    for line in lines:
        url = line if isinstance(line, str) else str((line or {}).get("url") or "")
        trimmed = url.strip()
        if trimmed and trimmed not in out:
            out.append(trimmed)
    return out


def normalize_line_pool(lines: Any) -> list[str]:
    """全局线路池归一化：存基地址，拼地址时用 `基地址/编号.m3u8`。

    对应 store.js:1341 `normalizeLinePool` —— 去掉结尾的 /，去空去重。
    """
    out: list[str] = []
    for line in lines or []:
        url = line if isinstance(line, str) else str((line or {}).get("url") or "")
        base = re.sub(r"/+$", "", url.strip())
        if base and base not in out:
            out.append(base)
    return out
