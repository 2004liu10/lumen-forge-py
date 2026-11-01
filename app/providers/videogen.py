"""视频生成适配器（图生视频）—— 逐协议复刻 `server/providers/videogen.js`。

三家都是「提交 → 轮询 → 拿视频地址 → 下载」的异步流程，但请求体形态差异过大
（智谱扁平、方舟数组、百炼嵌套），硬凑统一抽象只会更啰嗦。所以这里只抽两个通用件：

  - `video_request()` —— 带鉴权 / 超时 / 错误消息提取的 HTTP 封装
  - `download_video()` —— 把生成的 mp4 拉回本地

提交与查询各写各的，状态值在各自的 `query_*` 里就地翻译成统一的 running / success / failed。

⚠️ 火山方舟：图片生成是 POST {baseUrl}/images/generations，
   视频生成是 POST {baseUrl}/contents/generations/tasks —— baseUrl 和 API Key 完全一样，
   改代码时别把两者搞混（这是最容易踩的坑）。

本机服务监听在 127.0.0.1 上，厂商抓不到，所以输入图只能走 base64 data URI；
三家的视频接口都收 data URI，因此不需要把图片先传到公网。
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
from typing import Any

import httpx

log = logging.getLogger(__name__)

# ============================================================ 服务商预设

#: 各服务商的默认配置与参数候选；后端发给前端当预设（对应 PROVIDER_PRESETS）
PROVIDER_PRESETS: dict[str, dict[str, Any]] = {
    "zhipu": {
        "label": "智谱 BigModel",
        "baseUrl": "https://open.bigmodel.cn",
        "keyUrl": "https://bigmodel.cn/usercenter/proj-mgmt/apikeys",
        "keyLabel": "去智谱开放平台创建 API Key",
        "models": ["cogvideox-flash", "cogvideox-3"],
        "freeModel": "cogvideox-flash",
        "durations": ["5", "10"],
        # 智谱这一栏直接写「宽x高」，不是 720p 这类代号
        "resolutions": [
            "1024x1024", "1280x960", "960x1280", "1920x1080", "1080x1920",
            "720x480", "2048x1080", "3840x2160",
        ],
        "sizeLabels": {
            "1024x1024": "1:1",
            "1280x960": "4:3",
            "960x1280": "3:4",
            "1920x1080": "16:9",
            "1080x1920": "9:16",
            "720x480": "3:2",
            "2048x1080": "≈17:9",
            "3840x2160": "16:9（4K）",
        },
        "ratios": [],
        "maxImageBytes": 5 * 1024 * 1024,
        "note": "cogvideox-flash 目前免费，适合先跑通链路；单图 ≤5MB，时长只有 5 秒 / 10 秒",
    },
    "ark": {
        "label": "火山方舟 Seedance",
        "baseUrl": "https://ark.cn-beijing.volces.com/api/v3",
        "keyUrl": "https://console.volcengine.com/ark/region:ark+cn-beijing/apiKey",
        "keyLabel": "去火山方舟控制台创建 API Key",
        "models": [
            "doubao-seedance-1-0-lite-i2v-250428",
            "doubao-seedance-1-0-pro-250528",
            "doubao-seedance-2-0-mini-260615",
            "doubao-seedance-2-5-260628",
        ],
        "freeModel": "",
        "durations": ["5", "10"],
        "resolutions": ["480p", "720p", "1080p"],
        # 方舟是唯一能直接下发比例的一档
        "ratios": ["1:1", "4:3", "3:4", "16:9", "9:16", "21:9"],
        "maxImageBytes": 30 * 1024 * 1024,
        "note": "要先在方舟控制台开通对应模型（Seedance 2.x 要求余额 >200 元或已购资源包）；查询结果只保留 7 天",
    },
    "dashscope": {
        "label": "阿里云百炼（通义万相）",
        "baseUrl": "https://dashscope.aliyuncs.com",
        "keyUrl": "https://bailian.console.aliyun.com/?apiKey=1#/api-key",
        "keyLabel": "去阿里云百炼控制台创建 API Key",
        "models": [
            "wan2.2-kf2v-flash", "wan2.2-i2v-flash", "wan2.1-i2v-turbo",
            "wan2.1-i2v-plus", "wanx2.1-kf2v-plus",
        ],
        "freeModel": "wan2.2-i2v-flash",
        "durations": ["5"],
        "resolutions": ["480P", "720P", "1080P"],
        "ratios": [],
        "ratioNote": "比例跟随输入图（该接口只提供 480P/720P/1080P 分辨率档）",
        "maxImageBytes": 20 * 1024 * 1024,
        "note": (
            "新用户有免费额度；提交必须带 X-DashScope-Async: enable，漏传会报 does not support synchronous calls。"
            "首尾帧只认带 kf2v 的模型（wan2.2-kf2v-flash / wanx2.1-kf2v-plus），i2v 系列只吃首帧"
        ),
    },
    "doubao": {
        "label": "本地豆包（客户端桥）",
        # 这里当 CDP 调试端口用，不是 HTTP 接口地址
        "baseUrl": "http://127.0.0.1:9555",
        "keyUrl": "",
        "keyLabel": "",
        "models": ["doubao-local-video"],
        "durations": ["5", "10", "15", "20", "25", "30"],
        "resolutions": ["720P", "1080P"],
        "ratios": ["自动", "16:9", "9:16", "4:3", "3:4", "1:1", "21:9"],
        "tiers": [
            {"id": "2.5", "label": "2.5", "hint": "旗舰画质 · 5 倍消耗 · 最长 30 秒"},
            {"id": "2.0", "label": "2.0", "hint": "进阶画质 · 2 倍消耗 · 最长 30 秒"},
            {"id": "fast", "label": "Fast", "hint": "Seedance 2.0 Fast · 快速出片"},
            {"id": "mini", "label": "Mini", "hint": "Seedance 2.0 Mini · 日常省额度 · 最长 15 秒"},
        ],
        "maxImageBytes": 20 * 1024 * 1024,
        "note": (
            "不走 API、不花钱：驱动本机的豆包客户端出片，所以豆包必须开着且带调试端口。"
            "必须给首帧图（图生视频）。时长 / 清晰度 / 比例会直接设进豆包的面板（4~30 秒、720P/1080P），"
            "「额外参数」里写 {\"doubaoTier\":\"2.5\"} 可以顺手把模型档切过去（不写就跟随豆包当前选择）。"
            "出片约 2~6 分钟，30 秒的片子更久。"
        ),
    },
}

MIME_BY_EXT = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".gif": "image/gif",
}

#: 允许当输入图的格式（扩展名 → MIME），上传时用来拦掉非图片文件
INPUT_MIME_TYPES = MIME_BY_EXT

REQUEST_TIMEOUT_S = 2 * 60.0
DOWNLOAD_TIMEOUT_S = 5 * 60.0  # mp4 体积大，比图片留更久

DOUBAO_DEFAULT_ENDPOINT = "http://127.0.0.1:9555"
DOUBAO_VIDEO_URL_PREFIX = "doubao-bridge://"


class VideoGenError(Exception):
    """视频生成失败。`code` 是 HTTP 状态码（有的话），`raw` 是原始响应。"""

    def __init__(self, message: str, *, code: Any = None, raw: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.raw = raw


# ============================================================ 通用工具


def join_url(base_url: str | None, suffix: str) -> str:
    return f"{re.sub(r'/+$', '', str(base_url or ''))}{suffix}"


def fmt_mb(n: int) -> str:
    return f"{n / 1024 / 1024:.1f}MB"


def to_data_uri(buffer: bytes, mime_type: str | None) -> str:
    """本机文件 → base64 data URI；格式串一律小写"""
    mime = str(mime_type or "image/png").lower()
    return f"data:{mime};base64,{base64.b64encode(buffer).decode('ascii')}"


def read_input_data_uri(input_snap: dict | None, max_bytes: int) -> str:
    """把任务的输入图快照读成 data URI，并按服务商上限校验体积"""
    if not input_snap or not input_snap.get("localPath"):
        raise VideoGenError("这条任务没有输入图片")
    path = input_snap["localPath"]
    if not os.path.exists(path):
        raise VideoGenError("输入图片的本地文件已被删除，请重新选择")
    with open(path, "rb") as fh:
        buffer = fh.read()
    if max_bytes and len(buffer) > max_bytes:
        raise VideoGenError(
            f"输入图片 {fmt_mb(len(buffer))} 超过这家接口的上限 {fmt_mb(max_bytes)}，请换一张更小的图"
        )
    ext = os.path.splitext(path)[1].lower()
    mime = str(input_snap.get("mimeType") or MIME_BY_EXT.get(ext) or "image/png").lower()
    return to_data_uri(buffer, mime)


def apply_extra_params(body: dict, extra_params: Any) -> dict:
    """模型配置里的「额外参数」原样并入请求体，可覆盖任何默认字段"""
    extra = str(extra_params or "").strip()
    if not extra:
        return body
    try:
        parsed = json.loads(extra)
    except json.JSONDecodeError as err:
        raise VideoGenError(f"额外参数不是合法 JSON：{err}") from err
    if not isinstance(parsed, dict):
        raise VideoGenError('额外参数必须是 JSON 对象，例如 {"seed": 12345}')
    body.update(parsed)
    return body


def filled(value: Any) -> bool:
    """空字符串 / None 都当作「没填」，不要塞进请求体"""
    return value is not None and str(value).strip() != ""


def to_int(value: Any) -> int:
    return int(float(value))


async def video_request(
    *,
    url: str,
    api_key: str,
    method: str = "POST",
    body: dict | None = None,
    headers: dict[str, str] | None = None,
    timeout_s: float | None = None,
) -> dict:
    """带鉴权、超时与错误提取的请求封装。

    `headers` 用来塞厂商特有头（百炼的 X-DashScope-Async 就走这里）。
    """
    req_headers = {"authorization": f"Bearer {api_key}"}
    if body:
        req_headers["content-type"] = "application/json"
    if headers:
        req_headers.update(headers)

    try:
        async with httpx.AsyncClient(timeout=timeout_s or REQUEST_TIMEOUT_S, follow_redirects=True) as client:
            res = await client.request(
                method,
                url,
                headers=req_headers,
                content=json.dumps(body, ensure_ascii=False).encode("utf-8") if body else None,
            )
    except httpx.TimeoutException as err:
        raise VideoGenError("请求超时：视频接口长时间没有响应") from err
    except httpx.HTTPError as err:
        raise VideoGenError(f"无法连接视频接口：{err}") from err

    text = res.text
    parsed: Any = None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None

    if res.status_code >= 400:
        detail = ""
        if isinstance(parsed, dict):
            detail = str(
                (parsed.get("error") or {}).get("message")
                or (parsed.get("output") or {}).get("message")
                or parsed.get("message")
                or parsed.get("msg")
                or ""
            )
        if not detail:
            detail = text[:300] or f"HTTP {res.status_code}"
        raise VideoGenError(
            f"接口返回 {res.status_code}：{detail}", code=res.status_code, raw=parsed
        )
    if parsed is None:
        raise VideoGenError(f"接口返回的不是 JSON：{text[:300]}")
    return parsed


# ============================================================ 智谱 BigModel

#: 智谱视频接口 size 字段认的「宽x高」枚举
ZHIPU_SIZES = {
    "720x480", "1024x1024", "1280x960", "960x1280",
    "1920x1080", "1080x1920", "2048x1080", "3840x2160",
}


async def submit_zhipu(model: dict, *, prompt: str, params: dict, image_url: str) -> dict:
    body: dict[str, Any] = {"model": model.get("model"), "prompt": prompt, "watermark_enabled": False}
    if image_url:
        body["image_url"] = image_url
    if filled(params.get("duration")):
        body["duration"] = to_int(params["duration"])
    # 智谱这里要给「宽x高」；填了枚举之外的值（比如方舟式的 1080p）会被 400 拒收，干脆不下发
    if filled(params.get("resolution")) and str(params["resolution"]) in ZHIPU_SIZES:
        body["size"] = str(params["resolution"])
    if filled(params.get("seed")):
        body["seed"] = to_int(params["seed"])
    apply_extra_params(body, model.get("extraParams"))

    parsed = await video_request(
        url=join_url(model.get("baseUrl"), "/api/paas/v4/videos/generations"),
        api_key=model.get("apiKey") or "",
        body=body,
    )
    task_id = parsed.get("id") or parsed.get("request_id")
    if not task_id:
        raise VideoGenError(
            f"提交成功但没拿到任务 ID：{json.dumps(parsed, ensure_ascii=False)[:300]}", raw=parsed
        )
    return {"remoteTaskId": str(task_id)}


async def query_zhipu(model: dict, remote_task_id: str) -> dict:
    parsed = await video_request(
        url=join_url(
            model.get("baseUrl"),
            f"/api/paas/v4/async-result/{_quote(remote_task_id)}",
        ),
        api_key=model.get("apiKey") or "",
        method="GET",
    )
    raw = str(parsed.get("task_status") or "").upper()
    status = "success" if raw == "SUCCESS" else "failed" if raw == "FAIL" else "running"
    results = parsed.get("video_result") or []
    return {
        "status": status,
        "videoUrl": (results[0] or {}).get("url") if results else "",
        "error": str((parsed.get("error") or {}).get("message") or parsed.get("message") or "智谱返回任务失败")
        if status == "failed"
        else "",
        "raw": raw,
    }


# ============================================================ 火山方舟 Seedance


async def submit_ark(model: dict, *, prompt: str, params: dict, image_url: str, end_image_url: str) -> dict:
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    if image_url:
        content.append({"type": "image_url", "image_url": {"url": image_url}, "role": "first_frame"})
    # 尾帧只有 pro 档这类支持的模型才给，见 last_frame_supported() —— lite / mini 传了会直接 400
    if end_image_url:
        content.append({"type": "image_url", "image_url": {"url": end_image_url}, "role": "last_frame"})
    body: dict[str, Any] = {"model": model.get("model"), "content": content, "watermark": False}
    if filled(params.get("resolution")):
        body["resolution"] = str(params["resolution"])
    if filled(params.get("ratio")):
        body["ratio"] = str(params["ratio"])
    if filled(params.get("duration")):
        body["duration"] = to_int(params["duration"])
    if filled(params.get("seed")):
        body["seed"] = to_int(params["seed"])
    apply_extra_params(body, model.get("extraParams"))

    parsed = await video_request(
        # 注意：不是 /images/generations（那是图片生成）
        url=join_url(model.get("baseUrl"), "/contents/generations/tasks"),
        api_key=model.get("apiKey") or "",
        body=body,
    )
    if not parsed.get("id"):
        raise VideoGenError(
            f"提交成功但没拿到任务 ID：{json.dumps(parsed, ensure_ascii=False)[:300]}", raw=parsed
        )
    return {"remoteTaskId": str(parsed["id"])}


async def query_ark(model: dict, remote_task_id: str) -> dict:
    parsed = await video_request(
        url=join_url(
            model.get("baseUrl"),
            f"/contents/generations/tasks/{_quote(remote_task_id)}",
        ),
        api_key=model.get("apiKey") or "",
        method="GET",
    )
    raw = str(parsed.get("status") or "").lower()
    done = raw == "succeeded"
    dead = raw in ("failed", "expired", "cancelled")
    return {
        "status": "success" if done else "failed" if dead else "running",
        "videoUrl": (parsed.get("content") or {}).get("video_url") or "",
        "error": str((parsed.get("error") or {}).get("message") or f"方舟任务状态：{raw}") if dead else "",
        "raw": raw,
    }


# ============================================================ 阿里云百炼（万相）


def build_dashscope_request(
    model: dict, *, prompt: str, params: dict, image_url: str, end_image_url: str
) -> dict:
    """拼百炼（万相）的提交请求 —— 同一家底下有三套写法，认错了就白跑一趟还照样扣钱：

      - 名字里带 kf2v 的（wan2.2-kf2v-flash、wanx2.1-kf2v-plus）是**首尾帧专用档**：
        input.first_frame_url + input.last_frame_url，走 image2video 那条路径；
      - wan2.7 起改成 input.media 数组 [{type:'first_frame'|'last_frame', url}]；
      - i2v / t2v 这些只吃首帧的：字段名是 input.img_url。

    抽成纯函数是为了能直接单测 —— 这三套字段名在界面上完全看不出差别。
    """
    model_id = str(model.get("model") or "").lower()
    inp: dict[str, Any] = {"prompt": prompt}
    if re.search(r"wan2\.7", model_id, re.I):
        if image_url:
            inp["media"] = [{"type": "first_frame", "url": image_url}]
        if end_image_url and inp.get("media"):
            inp["media"].append({"type": "last_frame", "url": end_image_url})
    elif re.search(r"kf2v", model_id, re.I):
        if image_url:
            inp["first_frame_url"] = image_url
        if end_image_url:
            inp["last_frame_url"] = end_image_url
    elif image_url:
        inp["img_url"] = image_url

    body: dict[str, Any] = {"model": model.get("model"), "input": inp, "parameters": {"watermark": False}}
    if filled(params.get("resolution")):
        body["parameters"]["resolution"] = str(params["resolution"])
    if filled(params.get("duration")):
        body["parameters"]["duration"] = to_int(params["duration"])
    if filled(params.get("seed")):
        body["parameters"]["seed"] = to_int(params["seed"])
    apply_extra_params(body, model.get("extraParams"))
    path = (
        "/api/v1/services/aigc/image2video/video-synthesis"
        if re.search(r"kf2v", model_id, re.I)
        else "/api/v1/services/aigc/video-generation/video-synthesis"
    )
    return {"url": join_url(model.get("baseUrl"), path), "body": body}


async def submit_dashscope(
    model: dict, *, prompt: str, params: dict, image_url: str, end_image_url: str
) -> dict:
    req = build_dashscope_request(
        model, prompt=prompt, params=params, image_url=image_url, end_image_url=end_image_url
    )
    parsed = await video_request(
        url=req["url"],
        api_key=model.get("apiKey") or "",
        body=req["body"],
        # 漏了这个头，百炼会直接报 does not support synchronous calls
        headers={"X-DashScope-Async": "enable"},
    )
    task_id = (parsed.get("output") or {}).get("task_id")
    if not task_id:
        raise VideoGenError(
            f"提交成功但没拿到任务 ID：{json.dumps(parsed, ensure_ascii=False)[:300]}", raw=parsed
        )
    return {"remoteTaskId": str(task_id)}


async def query_dashscope(model: dict, remote_task_id: str) -> dict:
    parsed = await video_request(
        url=join_url(model.get("baseUrl"), f"/api/v1/tasks/{_quote(remote_task_id)}"),
        api_key=model.get("apiKey") or "",
        method="GET",
    )
    output = parsed.get("output") or {}
    raw = str(output.get("task_status") or "").upper()
    done = raw == "SUCCEEDED"
    dead = raw in ("FAILED", "CANCELED", "UNKNOWN")
    return {
        "status": "success" if done else "failed" if dead else "running",
        "videoUrl": output.get("video_url") or "",
        "error": str(output.get("message") or f"百炼任务状态：{raw}") if dead else "",
        "raw": raw,
    }


# ============================================================ 本地豆包（客户端桥）


def _doubao_extra(model: dict) -> dict:
    """额外参数里可以改写提示词前缀：{"videoPrefix": "请生成一段视频："}"""
    raw = str(model.get("extraParams") or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


async def submit_doubao(
    model: dict, *, prompt: str, params: dict, image: dict | None,
    end_image_file: str, on_progress=None,
) -> dict:
    """本地豆包：时长 / 比例 / 清晰度真的点进它的参数面板设。

    「额外参数」里可写：
      {"doubaoTier":"2.5"|"2.0"|"fast"|"mini"}  让桥顺手把模型档切过去
      {"videoPrefix":"…"}                       提示词前缀（默认不加）
    """
    from .doubao_bridge import submit_video_with_doubao

    extra = _doubao_extra(model)
    prefix = str(extra["videoPrefix"]) if "videoPrefix" in extra else ""
    # 节点上选过的档位优先（gen.doubaoTier → task.params.tier），没选才看「额外参数」
    tier = (
        str(params["tier"]) if filled(params.get("tier"))
        else str(extra["doubaoTier"]) if "doubaoTier" in extra
        else ""
    )
    res = await submit_video_with_doubao(
        prompt=f"{prefix}{prompt}",
        image_file=(image or {}).get("localPath") or "",
        # 第二张 = 这段结束时要落到的画面（豆包不认尾帧参数，只能当图像提示喂进去）
        end_image_file=end_image_file or "",
        options={
            "seconds": to_int(params["duration"]) if filled(params.get("duration")) else 0,
            "resolution": str(params["resolution"]) if filled(params.get("resolution")) else "",
            "ratio": str(params["ratio"]) if filled(params.get("ratio")) else "",
            "tier": tier,
        },
        endpoint=model.get("baseUrl") or DOUBAO_DEFAULT_ENDPOINT,
        on_progress=on_progress,
    )
    # 面板没设上（豆包改版 / 窗口不在视频模式）时，把想要的参数补进提示词兜一层
    applied = res.get("applied")
    note = ""
    if not applied and (filled(params.get("duration")) or filled(params.get("ratio"))):
        note = (
            f"参数没能设进豆包面板，这次实际时长 / 比例以豆包为准"
            f"（想要 {params.get('duration') or '?'} 秒、{params.get('ratio') or '自动'}）"
        )
    return {**res, "note": note}


async def query_doubao(model: dict, remote_task_id: str) -> dict:
    from .doubao_bridge import query_video_with_doubao

    return await query_video_with_doubao(
        endpoint=model.get("baseUrl") or DOUBAO_DEFAULT_ENDPOINT,
        remote_task_id=remote_task_id,
    )


# ============================================================ 对外接口

_SUBMITTERS = {
    "zhipu": submit_zhipu,
    "ark": submit_ark,
    "dashscope": submit_dashscope,
    "doubao": submit_doubao,
}
_QUERIERS = {
    "zhipu": query_zhipu,
    "ark": query_ark,
    "dashscope": query_dashscope,
    "doubao": query_doubao,
}


def _check_model(model: dict) -> dict:
    preset = PROVIDER_PRESETS.get(model.get("provider") or "")
    if not preset:
        raise VideoGenError(f"不认识的视频服务商：{model.get('provider') or '（空）'}")
    # 本地豆包桥不需要模型 ID 也不需要 API Key：它驱动的是本机那个豆包客户端
    if model.get("provider") == "doubao":
        return preset
    if not model.get("model"):
        raise VideoGenError("这个视频模型还没有填写模型 ID")
    if not model.get("apiKey"):
        raise VideoGenError(f"视频模型「{model.get('name')}」还没有填写 API Key")
    return preset


def last_frame_supported(model: dict) -> bool:
    """这个模型能不能吃「尾帧」。

    首尾帧是让相邻两张镜头图之间那段视频不跑偏的关键：首帧定起点、尾帧定终点。
    但不是每家都支持 —— lite / mini / 智谱 / 本地豆包传了只会收到 400。
    所以按模型 ID 认，认不出的当不支持（宁可不用，别报错）。
    """
    model_id = str(model.get("model") or "").lower()
    provider = str(model.get("provider") or "")
    if provider == "ark":
        return bool(re.search(r"seedance-1-0-pro|seedance-1-5-pro|seedance-2-0-pro|seedance-2-5", model_id, re.I))
    if provider == "dashscope":
        return bool(re.search(r"kf2v", model_id, re.I) or re.search(r"wan2\.7", model_id, re.I))
    return False


def takes_second_image(model: dict) -> bool:
    """这个模型值不值得喂第二张图（尾帧 / 目标画面）。

    两种「第二张」不是一回事，但都值得传：
      - 真尾帧参数（方舟 pro、万相 kf2v）—— 接口层面把最后一帧钉死；
      - 本地豆包 —— 没有尾帧参数，但它的上传口收多张图，第二张能当「这段要走到哪」的图像提示。
    """
    return str(model.get("provider") or "") == "doubao" or last_frame_supported(model)


async def submit_video(
    *, model: dict, prompt: str, params: dict, image: dict | None,
    end_image: dict | None, on_progress=None,
) -> dict:
    """提交一次生成，返回 {remoteTaskId}。on_progress 只有本地豆包桥会回调。"""
    preset = _check_model(model)
    text = str(prompt or "").strip()
    if not text:
        raise VideoGenError("请填写提示词")

    # 本地豆包桥要的是本机文件路径（CDP 直接塞 file input），不是 base64 data URI
    if model.get("provider") == "doubao":
        # 豆包没有尾帧参数，但上传口收多张图：第二张当「这段结束时要走到哪」的目标画面。
        # 真正保证连续的是末帧接力（下一段的首帧 = 这一段抽出来的真实末帧）
        end_path = ""
        if end_image and end_image.get("localPath") and os.path.exists(end_image["localPath"]):
            end_path = end_image["localPath"]
        return await submit_doubao(
            model, prompt=text, params=params or {}, image=image,
            end_image_file=end_path, on_progress=on_progress,
        )

    image_url = read_input_data_uri(image, preset["maxImageBytes"]) if image else ""
    # 尾帧：模型不认就干脆不读不传（读一遍大图再被 400 拒，白白浪费几秒）
    end_image_url = (
        read_input_data_uri(end_image, preset["maxImageBytes"])
        if end_image and last_frame_supported(model)
        else ""
    )
    submitter = _SUBMITTERS.get(model.get("provider") or "")
    if not submitter:
        raise VideoGenError(f"不认识的视频服务商：{model.get('provider') or '（空）'}")
    return await submitter(
        model, prompt=text, params=params or {}, image_url=image_url, end_image_url=end_image_url
    )


async def query_video(*, model: dict, remote_task_id: str) -> dict:
    """查一次任务状态，返回 {status: running|success|failed, videoUrl, error, raw}"""
    _check_model(model)
    if not remote_task_id:
        raise VideoGenError("这条任务还没有远端任务 ID")
    querier = _QUERIERS.get(model.get("provider") or "")
    if not querier:
        raise VideoGenError(f"不认识的视频服务商：{model.get('provider') or '（空）'}")
    return await querier(model, remote_task_id)


async def download_video(input_: Any) -> dict:
    """把生成的视频拉回本地，返回 {buffer, ext}。

    传 {model, url}：豆包的视频在页面里（blob:），Node 侧 fetch 不到，要走本地桥。
    """
    arg = {"url": input_} if isinstance(input_, str) else (input_ or {})
    model = arg.get("model")
    url = arg.get("url")
    if not url:
        raise VideoGenError("接口没有返回视频地址")

    if str(url).startswith(DOUBAO_VIDEO_URL_PREFIX):
        from .doubao_bridge import fetch_video_with_doubao

        return await fetch_video_with_doubao(
            endpoint=(model or {}).get("baseUrl") or DOUBAO_DEFAULT_ENDPOINT,
            src=str(url)[len(DOUBAO_VIDEO_URL_PREFIX):],
        )

    try:
        async with httpx.AsyncClient(timeout=DOWNLOAD_TIMEOUT_S, follow_redirects=True) as client:
            res = await client.get(str(url))
    except httpx.TimeoutException as err:
        raise VideoGenError("下载视频超时") from err
    except httpx.HTTPError as err:
        raise VideoGenError(f"下载视频失败：{err}") from err

    if res.status_code >= 400:
        raise VideoGenError(f"下载视频失败：HTTP {res.status_code}")
    buffer = res.content
    if not buffer:
        raise VideoGenError("下载到的内容为空")
    return {"buffer": buffer, "ext": ".mp4"}


def _quote(value: str) -> str:
    """URL 路径段转义（对应 JS 的 encodeURIComponent）"""
    from urllib.parse import quote

    return quote(str(value), safe="")
