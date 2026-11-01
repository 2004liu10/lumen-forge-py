"""图片生成适配器 —— 逐协议复刻 `server/providers/imagegen.js`。

支持的协议（`model.protocol` 决定走哪一套）：

  - ark       火山方舟（豆包 Seedream），POST {baseUrl}/images/generations，Bearer
  - openai    OpenAI 兼容图片接口，路径同样 /images/generations
  - zhipu     智谱 CogView，baseUrl 要配成 https://open.bigmodel.cn/api/paas/v4
  - jimeng    即梦 4.0（火山引擎智能视觉）：AK/SK V4 签名 + 异步提交/轮询
              apiKey 存 AccessKeyID，akSecret 存 AccessKeySecret
  - wanxiang  通义万相（阿里云百炼 DashScope），按模型 ID 自动分流三套接口
  - hunyuan   腾讯混元生图（TokenHub），Bearer + 同步返回
  - minimax   MiniMax 海螺，Bearer + 同步返回，参考图只收 1 张
  - cloudflare Cloudflare Workers AI（FLUX.2），multipart/form-data
  - doubao     本机豆包客户端桥（原版靠 Electron CDP 驱动界面，见 doubao_bridge.py）

图生图规则与 Node 版完全一致：参考图一律走 base64 data URI
（本机服务在 127.0.0.1 上，厂商抓不到，所以不能给本地 URL）。

去水印：方舟接口 watermark 默认 true，这里统一下发 false，拿到的是干净原图。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac as hmac_mod
import json
import logging
import mimetypes
import os
import re
import urllib.parse
from io import BytesIO
from typing import Any

import httpx

from ..config import MAX_COUNT, MAX_REFS, MAX_REF_BYTES  # noqa: F401  (对外沿用同名常量)

log = logging.getLogger(__name__)

# ============================================================ 常量与预设

ARK_BASE = "https://ark.cn-beijing.volces.com/api/v3"

#: 方舟在售的图片生成模型 ID（供界面候选提示，实际以控制台开通情况为准）
ARK_MODEL_PRESETS = [
    "doubao-seedream-5-0-pro-260628",
    "doubao-seedream-5-0-flash-260915",
    "doubao-seedream-5-0-260128",
    "doubao-seedream-4-5-251128",
    "doubao-seedream-4-0-250828",
]

#: 常用尺寸候选：方舟支持 1K/2K/3K/4K 或「宽x高」，OpenAI 兼容接口用「宽x高」
SIZE_PRESETS = ["1K", "2K", "3K", "4K", "1024x1024", "1536x1024", "1024x1536", "2048x2048"]

# ------------------------------------------------------------------ 智谱
ZHIPU_BASE = "https://open.bigmodel.cn/api/paas/v4"
ZHIPU_MODEL_PRESETS = ["cogview-3-flash", "cogview-4", "cogview-4-250304", "glm-image"]

#: cogview 系尺寸枚举（宽x高）。填枚举外的值接口会 400 拒收，所以只在命中枚举时下发
ZHIPU_IMAGE_SIZES = {
    "1024x1024", "768x1344", "864x1152", "1344x768", "1152x864", "1440x720", "720x1440",
}
ZHIPU_SIZE_PRESETS = sorted(ZHIPU_IMAGE_SIZES)

# ------------------------------------------------------------------ 即梦
JIMENG_BASE = "https://visual.volcengineapi.com"
JIMENG_HOST = "visual.volcengineapi.com"
JIMENG_REGION = "cn-north-1"
JIMENG_SERVICE = "cv"
JIMENG_REQ_KEY = "jimeng_t2i_v40"
#: 即梦「图生图 3.0 智能参考」的 req_key：带参考图时自动改用这个
JIMENG_I2I_REQ_KEY = "jimeng_i2i_v30"
JIMENG_API_VERSION = "2022-08-31"
JIMENG_ACTION_SUBMIT = "CVSync2AsyncSubmitTask"
JIMENG_ACTION_QUERY = "CVSync2AsyncGetResult"

JIMENG_SIZE_PRESETS = [
    "1024x1024", "2048x2048", "4096x4096",
    "2304x1728", "1728x2304", "4694x3520", "3520x4694",
    "2496x1664", "1664x2496", "4992x3328", "3328x4992",
    "2560x1440", "1440x2560", "5404x3040", "3040x5404",
    "3024x1296", "6198x2656",
]
JIMENG_MODEL_PRESETS = ["jimeng_t2i_v40", "jimeng_i2i_v30"]
JIMENG_POLL_INTERVAL_S = 3.0
JIMENG_POLL_TIMEOUT_S = 8 * 60.0

# ------------------------------------------------------------------ 通义万相
WANXIANG_BASE = "https://dashscope.aliyuncs.com"
WANXIANG_MODEL_PRESETS = [
    "wan2.7-image-pro",
    "wan2.6-image",
    "wan2.5-i2i-preview",
    "wanx2.1-imageedit",
    "wan2.2-t2i-flash",
    "wan2.2-t2i-plus",
    "wanx2.1-t2i-turbo",
    "wanx2.1-t2i-plus",
]
#: 命中这些前缀的万相模型走新版「多模态生成」同步接口
WANXIANG_MM_PREFIXES = ["wan2.5-i2i", "wan2.6", "wan2.7"]
#: 命中这些前缀的万相模型是文生图模型（走 text2image 异步接口，不要参考图）
WANXIANG_T2I_PREFIXES = ["wan2.2-t2i", "wan2.1-t2i", "wanx2.1-t2i", "wanx2.2-t2i"]
WANXIANG_FUNCTIONS = [
    "description_edit", "stylization_all", "stylization_local",
    "description_edit_with_mask", "remove_watermark", "expand",
    "super_resolution", "colorization", "doodle", "control_cartoon_feature",
]
WANXIANG_POLL_INTERVAL_S = 3.0
WANXIANG_POLL_TIMEOUT_S = 8 * 60.0

# ------------------------------------------------------------------ 腾讯混元
HUNYUAN_BASE = "https://tokenhub.tencentmaas.com"
HUNYUAN_MODEL_PRESETS = ["hy-image-v3", "hy-image-v3.5-preview"]
HUNYUAN_SIZE_PRESETS = ["1024:1024", "768:1344", "1344:768", "1280:720", "720:1280"]

# ------------------------------------------------------------------ MiniMax
MINIMAX_BASE = "https://api.minimax.cn"
MINIMAX_MODEL_PRESETS = ["image-01", "image-01-live"]
MINIMAX_SIZE_PRESETS = ["1:1", "16:9", "4:3", "3:2", "2:3", "3:4", "9:16", "21:9"]

# ------------------------------------------------------------- Cloudflare
CLOUDFLARE_BASE = "https://api.cloudflare.com"
CLOUDFLARE_MODEL_PRESETS = [
    "@cf/black-forest-labs/flux-2-klein-4b",
    "@cf/black-forest-labs/flux-2-klein-9b",
    "@cf/black-forest-labs/flux-2-dev",
]
CLOUDFLARE_SIZE_PRESETS = [
    "1024x1024", "1024x768", "768x1024", "1152x768", "768x1152", "1280x720", "720x1280",
]
#: FLUX.2 参考图上限（官方支持 input_image_0..3 共 4 张）
CLOUDFLARE_MAX_REFS = 4

#: 图片「比例 → 该比例下的尺寸」对照表（按协议），界面「比例」下拉直接用它。
#: 数组顺序 = 界面清晰度顺序，第 1 个是默认值；对象键顺序 = 比例下拉顺序。
IMAGE_RATIO_PRESETS: dict[str, dict[str, list[str]]] = {
    "ark": {
        "1:1": ["2048x2048", "4096x4096"],
        "4:3": ["2304x1728"],
        "3:4": ["1728x2304"],
        "3:2": ["2496x1664"],
        "2:3": ["1664x2496"],
        "16:9": ["2560x1440"],
        "9:16": ["1440x2560"],
        "21:9": ["3024x1296"],
    },
    "openai": {
        "1:1": ["1024x1024", "2048x2048"],
        "3:2": ["1536x1024"],
        "2:3": ["1024x1536"],
    },
    "zhipu": {
        "1:1": ["1024x1024"],
        "4:3": ["1152x864"],
        "3:4": ["864x1152"],
        "16:9": ["1344x768"],
        "9:16": ["768x1344"],
        "2:1": ["1440x720"],
        "1:2": ["720x1440"],
    },
    "jimeng": {
        "1:1": ["2048x2048", "4096x4096", "1024x1024"],
        "4:3": ["2304x1728", "4694x3520"],
        "3:4": ["1728x2304", "3520x4694"],
        "3:2": ["2496x1664", "4992x3328"],
        "2:3": ["1664x2496", "3328x4992"],
        "16:9": ["2560x1440", "5404x3040"],
        "9:16": ["1440x2560", "3040x5404"],
        "21:9": ["3024x1296", "6198x2656"],
    },
    "wanxiang": {
        "1:1": ["1024x1024"],
        "16:9": ["1280x720"],
        "9:16": ["720x1280"],
    },
    "hunyuan": {
        "1:1": ["1024:1024"],
        "16:9": ["1344:768", "1280:720"],
        "9:16": ["768:1344", "720:1280"],
    },
    "minimax": {
        "1:1": ["1:1"], "4:3": ["4:3"], "3:4": ["3:4"],
        "3:2": ["3:2"], "2:3": ["2:3"], "16:9": ["16:9"],
        "9:16": ["9:16"], "21:9": ["21:9"],
    },
    "cloudflare": {
        "1:1": ["1024x1024"],
        "4:3": ["1024x768"],
        "3:4": ["768x1024"],
        "3:2": ["1152x768"],
        "2:3": ["768x1152"],
        "16:9": ["1280x720"],
        "9:16": ["720x1280"],
    },
}

#: 方舟允许的参考图格式与其 MIME
REF_MIME_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".gif": "image/gif",
}

REQUEST_TIMEOUT_S = 5 * 60.0
DOWNLOAD_TIMEOUT_S = 3 * 60.0


# ============================================================ 错误类型


class ImageGenError(Exception):
    """生图链路里的可预期错误（消息直接展示给用户）。

    对应 Node 版 `class ImageGenError extends Error`：带 `code` 与 `raw`
    两个附加字段，上层路由据此拼响应。
    """

    def __init__(self, message: str, *, code: Any = None, raw: Any = None) -> None:
        super().__init__(message)
        self.name = "ImageGenError"
        self.code = code
        self.raw = raw


# ============================================================ 参考图工具


def is_url_ref(ref: dict | None) -> bool:
    """参考图快照是否是一张外网链接图（即梦这类只收公网 URL 的协议用）"""
    return bool((ref or {}).get("url"))


def to_data_uri(buffer: bytes, mime_type: str | None) -> str:
    """本机文件 → base64 data URI；格式串一律小写"""
    mime = str(mime_type or "image/png").lower()
    return f"data:{mime};base64,{base64.b64encode(buffer).decode('ascii')}"


def read_refs_data_uris(refs: list[dict] | None) -> list[str]:
    """把任务上的参考图快照读成 data URI 数组；文件缺失的直接跳过"""
    out: list[str] = []
    for ref in refs or []:
        local = (ref or {}).get("localPath")
        if not local or not os.path.exists(local):
            continue
        mime = (ref or {}).get("mimeType") or REF_MIME_TYPES.get(
            os.path.splitext(local)[1].lower(), "image/png"
        )
        try:
            with open(local, "rb") as fh:
                out.append(to_data_uri(fh.read(), mime))
        except OSError as err:
            log.warning("[imagegen] 参考图读取失败 %s：%s", local, err)
            continue
        if len(out) >= MAX_REFS:
            break
    return out


def split_data_uri(uri: str) -> dict[str, str]:
    """`data:image/png;base64,xxx` → {mimeType, base64}；不是 data URI 时 base64 原样返回"""
    m = re.match(r"^data:([^;,]+);base64,(.*)$", str(uri or ""), re.S)
    if not m:
        return {"mimeType": "", "base64": str(uri or "")}
    return {"mimeType": m.group(1), "base64": m.group(2)}


def parse_wxh(size: Any) -> dict[str, int] | None:
    """「宽x高」→ {width, height}；格式不对返回 None"""
    m = re.match(r"^(\d{2,5})\s*[xX×]\s*(\d{2,5})$", str(size or "").strip())
    if not m:
        return None
    return {"width": int(m.group(1)), "height": int(m.group(2))}


def normalize_colon_size(size: Any) -> str:
    """「宽:高」→ 规范化字符串；格式不对返回空（混元生图用冒号分隔）"""
    m = re.match(r"^(\d{2,5})\s*[:：]\s*(\d{2,5})$", str(size or "").strip())
    return f"{int(m.group(1))}:{int(m.group(2))}" if m else ""


def parse_size_or_ratio(size: Any) -> dict[str, Any]:
    """「宽x高」给宽高、「a:b」给比例，两种写法都收；都不是则返回空字典"""
    wh = parse_wxh(size)
    if wh:
        return {"width": wh["width"], "height": wh["height"]}
    ratio = normalize_colon_size(size)
    return {"aspectRatio": ratio} if ratio else {}


def read_extra_params(model: dict, example: str = '{"seed": 12345}') -> dict | None:
    """读取模型上的「额外参数」（JSON 对象）；空返回 None，非法抛 ImageGenError"""
    extra = str(model.get("extraParams") or "").strip()
    if not extra:
        return None
    try:
        parsed = json.loads(extra)
    except json.JSONDecodeError as err:
        raise ImageGenError(f"额外参数不是合法 JSON：{err}") from err
    if not isinstance(parsed, dict):
        raise ImageGenError(f"额外参数必须是 JSON 对象，例如 {example}")
    return parsed


def join_url(base_url: str | None, suffix: str) -> str:
    return f"{re.sub(r'/+$', '', str(base_url or ARK_BASE))}{suffix}"


# ============================================================ 火山引擎 V4 签名


def rfc3986_encode(value: Any) -> str:
    """RFC3986 编码：encodeURIComponent 基础上补齐 !'()* 的转义"""
    out = urllib.parse.quote(str(value), safe="-_.~")
    return out


def sha256_hex(data: bytes | str) -> str:
    raw = data.encode("utf-8") if isinstance(data, str) else data
    return hashlib.sha256(raw).hexdigest()


def _hmac(key: bytes | str, data: bytes | str) -> bytes:
    k = key.encode("utf-8") if isinstance(key, str) else key
    d = data.encode("utf-8") if isinstance(data, str) else data
    return hmac_mod.new(k, d, hashlib.sha256).digest()


def volc_authorization(
    *,
    method: str,
    host: str,
    path: str,
    query: dict[str, str],
    payload: str,
    access_key_id: str,
    access_key_secret: str,
    region: str,
    service: str,
    x_date: str,
) -> str:
    """计算火山引擎 V4 签名的 Authorization 头（HMAC-SHA256）。

    独立函数便于用官方文档示例值做离线单测。
    """
    canonical_query = "&".join(
        f"{k}={v}"
        for k, v in sorted(
            ((rfc3986_encode(k), rfc3986_encode(v)) for k, v in (query or {}).items()),
            key=lambda kv: kv[0],
        )
    )

    # 固定只签 host 与 x-date 两个头
    canonical_headers = f"host:{host}\nx-date:{x_date}\n"
    signed_headers = "host;x-date"

    canonical_request = "\n".join([
        method,
        path or "/",
        canonical_query,
        canonical_headers,
        signed_headers,
        sha256_hex(payload or ""),
    ])

    date = x_date[:8]
    credential_scope = f"{date}/{region}/{service}/request"
    string_to_sign = "\n".join([
        "HMAC-SHA256",
        x_date,
        credential_scope,
        sha256_hex(canonical_request),
    ])

    # 链式派生：每步 HMAC 输出的二进制原样作为下一步的 key
    k_date = _hmac(access_key_secret, date)
    k_region = _hmac(k_date, region)
    k_service = _hmac(k_region, service)
    k_signing = _hmac(k_service, "request")
    signature = hmac_mod.new(k_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    return (
        f"HMAC-SHA256 Credential={access_key_id}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )


def _utc_x_date() -> str:
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    return now.strftime("%Y%m%dT%H%M%SZ")


async def volc_post(
    *,
    host: str,
    region: str,
    service: str,
    action: str,
    version: str,
    body: dict,
    access_key_id: str,
    access_key_secret: str,
) -> dict:
    """向火山引擎 OpenAPI 发一个签名过的 POST（application/json）"""
    payload = json.dumps(body, ensure_ascii=False)
    x_date = _utc_x_date()
    query = {"Action": action, "Version": version}
    canonical_query = "&".join(
        f"{k}={v}"
        for k, v in sorted(
            ((rfc3986_encode(k), rfc3986_encode(v)) for k, v in query.items()),
            key=lambda kv: kv[0],
        )
    )
    authorization = volc_authorization(
        method="POST", host=host, path="/", query=query, payload=payload,
        access_key_id=access_key_id, access_key_secret=access_key_secret,
        region=region, service=service, x_date=x_date,
    )

    url = f"https://{host}/?{canonical_query}"
    headers = {
        "content-type": "application/json",
        "host": host,
        "x-date": x_date,
        "authorization": authorization,
    }
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S) as client:
        try:
            res = await client.post(url, headers=headers, content=payload.encode("utf-8"))
        except httpx.TimeoutException as err:
            raise ImageGenError("请求超时：即梦接口长时间没有响应") from err
        except httpx.HTTPError as err:
            raise ImageGenError(f"无法连接即梦接口：{err}") from err

    text = res.text
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None

    if res.status_code >= 400:
        detail = (
            (parsed or {}).get("message")
            or ((parsed or {}).get("error") or {}).get("message")
            or text[:300]
            or f"HTTP {res.status_code}"
        )
        raise ImageGenError(f"即梦接口返回 {res.status_code}：{detail}", code=res.status_code, raw=parsed)
    if parsed is None:
        raise ImageGenError(f"即梦接口返回的不是 JSON：{text[:300]}")
    return parsed


def parse_jimeng_size(size: Any) -> dict[str, int] | None:
    """解析「宽x高」；非法或不在候选里返回 None（不下发，走平台默认）"""
    m = re.match(r"^(\d{3,5})\s*[xX×]\s*(\d{3,5})$", str(size or "").strip())
    if not m:
        return None
    w, h = int(m.group(1)), int(m.group(2))
    if f"{w}x{h}" not in JIMENG_SIZE_PRESETS:
        return None
    return {"width": w, "height": h}


async def generate_one_jimeng(
    *, model: dict, prompt: str, size: str, ref_urls: list[str], ref_base64: list[str]
) -> list[dict]:
    """即梦单次生成：提交 → 轮询 → 返回 [{url}] 或 [{b64}]。

    有参考图时自动走「图生图 3.0」（jimeng_i2i_v30），无图走文生图（jimeng_t2i_v40）。
    """
    urls = (ref_urls or [])[:1]
    b64 = (ref_base64 or [])[:1]
    has_image = bool(urls) or bool(b64)
    req_key = JIMENG_I2I_REQ_KEY if has_image else JIMENG_REQ_KEY
    if has_image and not str(prompt or "").strip():
        raise ImageGenError("即梦图生图必须写提示词（告诉它要怎么改这张图）")

    body: dict[str, Any] = {"req_key": req_key, "prompt": prompt}
    if has_image:
        # 图生图 3.0 只收 1 张图：公网链接走 image_urls，本机图走 binary_data_base64。
        # 官方定义 binary_data_base64 是「纯 base64 的字符串数组」，不带 data: 前缀
        if urls:
            body["image_urls"] = urls
        else:
            body["binary_data_base64"] = [split_data_uri(u)["base64"] for u in b64]
        # 出图尺寸跟随底图，不下发 width/height
    else:
        wh = parse_jimeng_size(size)
        if wh:
            body["width"] = wh["width"]
            body["height"] = wh["height"]
        # 一张一提交，强制单图出图，避免平台按组图逻辑处理
        body["force_single"] = True

    try:
        submit_res = await volc_post(
            host=JIMENG_HOST, region=JIMENG_REGION, service=JIMENG_SERVICE,
            action=JIMENG_ACTION_SUBMIT, version=JIMENG_API_VERSION, body=body,
            access_key_id=model.get("apiKey") or "", access_key_secret=model.get("akSecret") or "",
        )
    except ImageGenError as err:
        # 图生图 3.0 是单独开通的服务：没开通时签名没问题，平台只甩 401 Access Denied
        if has_image and re.search(r"401|Access\s*Denied", str(err), re.I):
            raise ImageGenError(
                "即梦「图生图 3.0」这个账号还没开通（平台返回 401 Access Denied）："
                "去火山引擎控制台的「智能视觉 → 即梦AI」开通图生图 3.0；"
                "或者去掉参考图改用文生图（文生图 4.0 是通的）",
                code=err.code, raw=err.raw,
            ) from err
        raise

    if int(submit_res.get("code") or 0) != 10000 or not (submit_res.get("data") or {}).get("task_id"):
        raise ImageGenError(
            f"即梦提交任务失败（code={submit_res.get('code')}）："
            f"{submit_res.get('message') or json.dumps(submit_res, ensure_ascii=False)[:300]}",
            code=submit_res.get("code"), raw=submit_res,
        )
    task_id = submit_res["data"]["task_id"]

    deadline = asyncio.get_running_loop().time() + JIMENG_POLL_TIMEOUT_S
    while True:
        await asyncio.sleep(JIMENG_POLL_INTERVAL_S)
        query_res = await volc_post(
            host=JIMENG_HOST, region=JIMENG_REGION, service=JIMENG_SERVICE,
            action=JIMENG_ACTION_QUERY, version=JIMENG_API_VERSION,
            body={"req_key": req_key, "task_id": task_id},
            access_key_id=model.get("apiKey") or "", access_key_secret=model.get("akSecret") or "",
        )
        data = query_res.get("data") or {}
        status = str(data.get("status") or "").lower()
        if status == "done":
            if int(query_res.get("code") or 0) != 10000:
                raise ImageGenError(
                    f"即梦任务完成但返回异常（code={query_res.get('code')}）：{query_res.get('message') or ''}",
                    code=query_res.get("code"), raw=query_res,
                )
            images = extract_jimeng_images(data)
            if not images:
                raise ImageGenError(
                    f"即梦任务没有返回图片：{json.dumps(query_res, ensure_ascii=False)[:300]}"
                )
            return images
        if status in ("not_found", "expired"):
            raise ImageGenError(
                f"即梦任务{'已过期' if status == 'expired' else '不存在'}（task_id={task_id}）"
            )
        # processing / in_queue / generating：继续等
        if asyncio.get_running_loop().time() > deadline:
            raise ImageGenError(f"即梦任务等待超时（task_id={task_id}，最后状态 {status or '未知'}）")


def extract_jimeng_images(data: dict) -> list[dict]:
    """结果图字段兼容解析，返回 [{url}] 或 [{b64}]：

      - image_urls：公网链接（24 小时后失效）
      - binary_data_base64：图片本体 base64（即梦文生图 4.0 实测走这个字段）
      - resp_data：老接口把上面两者塞在一个 JSON 串里
    """
    out: list[dict] = []

    def add_urls(items: Any) -> None:
        for u in items or []:
            if isinstance(u, str) and u:
                out.append({"url": u})

    def add_base64(items: Any) -> None:
        for b in items or []:
            if isinstance(b, str) and b:
                out.append({"b64": split_data_uri(b)["base64"]})

    def collect(node: Any) -> None:
        if not isinstance(node, dict):
            return
        if isinstance(node.get("image_urls"), list):
            add_urls(node["image_urls"])
        if isinstance(node.get("binary_data_base64"), list):
            add_base64(node["binary_data_base64"])

    collect(data)
    resp = data.get("resp_data")
    if isinstance(resp, str) and resp:
        try:
            collect(json.loads(resp))
        except json.JSONDecodeError:
            pass
    elif isinstance(resp, dict):
        collect(resp)
    return out


# ============================================================ 通义万相


async def dashscope_request(
    *, base_url: str | None, api_key: str, method: str, path: str,
    body: dict | None = None, async_flag: bool = False,
) -> dict:
    """向百炼发一个 Bearer 鉴权请求；async_flag=True 时带 X-DashScope-Async: enable"""
    url = join_url(base_url or WANXIANG_BASE, path)
    headers = {"content-type": "application/json", "authorization": f"Bearer {api_key}"}
    if async_flag:
        headers["X-DashScope-Async"] = "enable"

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S) as client:
        try:
            res = await client.request(
                method, url, headers=headers,
                content=json.dumps(body, ensure_ascii=False).encode("utf-8") if body else None,
            )
        except httpx.TimeoutException as err:
            raise ImageGenError("请求超时：通义万相接口长时间没有响应") from err
        except httpx.HTTPError as err:
            raise ImageGenError(f"无法连接通义万相接口：{err}") from err

    text = res.text
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None

    if res.status_code >= 400:
        detail = (
            (parsed or {}).get("message")
            or (parsed or {}).get("code")
            or text[:300]
            or f"HTTP {res.status_code}"
        )
        raise ImageGenError(f"通义万相接口返回 {res.status_code}：{detail}", code=res.status_code, raw=parsed)
    if parsed is None:
        raise ImageGenError(f"通义万相接口返回的不是 JSON：{text[:300]}")
    return parsed


def extract_wanxiang_image_urls(output: dict | None) -> list[str]:
    """结果图字段兼容解析：output.results[].url / output.choices[].message.content[].image"""
    out: list[str] = []
    for r in (output or {}).get("results") or []:
        if isinstance(r, dict) and isinstance(r.get("url"), str) and r["url"]:
            out.append(r["url"])
    for c in (output or {}).get("choices") or []:
        content = ((c or {}).get("message") or {}).get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("image"), str) and part["image"]:
                    out.append(part["image"])
    return out


async def generate_one_wanxiang(
    *, model: dict, prompt: str, count: int, base_image: str
) -> list[dict]:
    """万相一次图生图：提交 → 轮询 → 返回 [{url}]。

    base_image 是 base64 data URI 或公网 URL（必填，图生图专用模型）；n 支持 1-4。
    """
    n = max(1, min(MAX_COUNT, int(count or 1)))
    parsed_extra = read_extra_params(model, '{"input":{"function":"remove_watermark"}}')

    body: dict[str, Any] = {
        "model": model.get("model"),
        "input": {"function": "description_edit", "prompt": prompt, "base_image_url": base_image},
        "parameters": {"n": n, "watermark": model.get("noWatermark") is False},
    }
    # 额外参数按根层合并（可整体覆盖 input / parameters 里的字段切换功能）
    if parsed_extra:
        body.update(parsed_extra)

    submit_res = await dashscope_request(
        base_url=model.get("baseUrl"), api_key=model.get("apiKey") or "",
        method="POST", path="/api/v1/services/aigc/image2image/image-synthesis",
        body=body, async_flag=True,
    )
    task_id = (submit_res.get("output") or {}).get("task_id")
    if not task_id:
        raise ImageGenError(
            f"通义万相提交任务失败：{submit_res.get('message') or submit_res.get('code') or json.dumps(submit_res, ensure_ascii=False)[:300]}",
            code=submit_res.get("code"), raw=submit_res,
        )
    return await wanxiang_await_result(model=model, task_id=task_id)


async def generate_one_wanxiang_t2i(
    *, model: dict, prompt: str, size: str, count: int
) -> list[dict]:
    """万相文生图（无参考图）：走 text2image 异步接口。

    size 命中「宽x高」才下发（百炼要的是 `宽*高`），否则交给平台自选。
    """
    n = max(1, min(MAX_COUNT, int(count or 1)))
    body: dict[str, Any] = {
        "model": model.get("model"),
        "input": {"prompt": prompt},
        "parameters": {"n": n, "watermark": model.get("noWatermark") is False},
    }
    wh = parse_wxh(size)
    if wh:
        body["parameters"]["size"] = f"{wh['width']}*{wh['height']}"
    extra = read_extra_params(model, '{"parameters":{"size":"1024*1024"}}')
    if extra:
        body.update(extra)

    submit_res = await dashscope_request(
        base_url=model.get("baseUrl"), api_key=model.get("apiKey") or "",
        method="POST", path="/api/v1/services/aigc/text2image/image-synthesis",
        body=body, async_flag=True,
    )
    task_id = (submit_res.get("output") or {}).get("task_id")
    if not task_id:
        raise ImageGenError(
            f"通义万相提交任务失败：{submit_res.get('message') or submit_res.get('code') or json.dumps(submit_res, ensure_ascii=False)[:300]}",
            code=submit_res.get("code"), raw=submit_res,
        )
    return await wanxiang_await_result(model=model, task_id=task_id)


async def wanxiang_await_result(*, model: dict, task_id: str) -> list[dict]:
    """万相异步任务统一轮询：拿到 task_id 后等结果，成功返回 [{url}]"""
    deadline = asyncio.get_running_loop().time() + WANXIANG_POLL_TIMEOUT_S
    while True:
        await asyncio.sleep(WANXIANG_POLL_INTERVAL_S)
        query_res = await dashscope_request(
            base_url=model.get("baseUrl"), api_key=model.get("apiKey") or "",
            method="GET", path=f"/api/v1/tasks/{urllib.parse.quote(str(task_id), safe='')}",
        )
        output = query_res.get("output") or {}
        status = str(output.get("task_status") or "").upper()
        if status == "SUCCEEDED":
            urls = extract_wanxiang_image_urls(output)
            if not urls:
                raise ImageGenError(
                    f"通义万相任务没有返回图片：{json.dumps(query_res, ensure_ascii=False)[:300]}"
                )
            return [{"url": u} for u in urls]
        if status in ("FAILED", "CANCELED", "UNKNOWN"):
            raise ImageGenError(
                f"通义万相任务{'失败' if status == 'FAILED' else '已取消'}："
                f"{output.get('message') or query_res.get('message') or status}",
                raw=query_res,
            )
        # PENDING / RUNNING：继续等
        if asyncio.get_running_loop().time() > deadline:
            raise ImageGenError(f"通义万相任务等待超时（task_id={task_id}，最后状态 {status or '未知'}）")


async def generate_one_wanxiang_mm(
    *, model: dict, prompt: str, size: str, count: int, refs: list[str]
) -> list[dict]:
    """万相 2.5-i2i / 2.6 / 2.7：新版「多模态生成」同步接口，一次请求直接出图。

    refs 是参考图数组（base64 data URI 或公网 URL），支持多张。
    """
    n = max(1, min(MAX_COUNT, int(count or 1)))
    lst = (refs or [])[:MAX_REFS]
    if not lst:
        raise ImageGenError("万相图像编辑需要至少 1 张参考图")

    content: list[dict[str, str]] = [{"text": prompt}]
    for u in lst:
        content.append({"image": u})
    body: dict[str, Any] = {
        "model": model.get("model"),
        "input": {"messages": [{"role": "user", "content": content}]},
        "parameters": {"n": n, "watermark": model.get("noWatermark") is False},
    }
    if size:
        body["parameters"]["size"] = str(size)
    extra = read_extra_params(model, '{"parameters":{"size":"2K"}}')
    if extra:
        body.update(extra)

    res = await dashscope_request(
        base_url=model.get("baseUrl"), api_key=model.get("apiKey") or "",
        method="POST", path="/api/v1/services/aigc/multimodal-generation/generation", body=body,
    )
    urls = extract_wanxiang_image_urls(res.get("output") or {})
    if not urls:
        raise ImageGenError(
            f"万相没有返回图片：{res.get('message') or res.get('code') or json.dumps(res, ensure_ascii=False)[:300]}",
            code=res.get("code"), raw=res,
        )
    return [{"url": u} for u in urls]


# ============================================================ 混元 / 海螺


async def generate_one_hunyuan(
    *, model: dict, prompt: str, size: str, refs: list[str]
) -> list[dict]:
    """腾讯混元生图（TokenHub）：Bearer + 同步返回，无需轮询。

    v3 与 v3.5 走不同端点，按模型 ID 自动分流；参考图最多 3 张（公网 URL 或 base64）。
    """
    text = str(prompt or "").strip()
    if not text:
        raise ImageGenError("混元生图必须填写提示词")
    base = re.sub(r"/+$", "", str(model.get("baseUrl") or HUNYUAN_BASE))
    v35 = "v3.5" in str(model.get("model") or "")
    endpoint = join_url(
        base,
        "/v1/wand/hunyuan-image/v35-generation" if v35 else "/v1/wand/hunyuan-image/v3-generation",
    )

    body: dict[str, Any] = {"model": model.get("model"), "prompt": text}
    lst = (refs or [])[:3]
    if lst:
        body["images"] = lst
    sz = normalize_colon_size(size)
    if sz:
        body["size"] = sz
    extra = read_extra_params(model, '{"seed":1234,"revise":true}')
    if extra:
        body.update(extra)

    parsed = await post_json(endpoint, model.get("apiKey") or "", body)
    items = extract_items(parsed)
    # v3.5 走 OpenAI 结构，图片藏在 choices[0].delta.image.url
    choices = parsed.get("choices") or [{}]
    nested = (
        ((choices[0] or {}).get("delta") or {}).get("image") or {}
    ).get("url") or (
        ((choices[0] or {}).get("message") or {}).get("image") or {}
    ).get("url")
    if not items and nested:
        items.append({"url": str(nested)})
    if not items:
        detail = (
            ((parsed.get("error") or {}).get("message"))
            or parsed.get("message")
            or json.dumps(parsed, ensure_ascii=False)[:300]
        )
        raise ImageGenError(f"混元生图没有返回图片：{detail}", raw=parsed)
    return items


async def generate_one_minimax(
    *, model: dict, prompt: str, size: str, count: int, ref: str | None
) -> list[dict]:
    """MiniMax 海螺图片生成：Bearer + 同步返回。

    参考图只收 1 张，走 subject_reference[].image_file（公网 URL 或 data URI 都行）。
    """
    text = str(prompt or "").strip()
    if not text:
        raise ImageGenError("海螺生图必须填写提示词")
    base = re.sub(r"/+$", "", str(model.get("baseUrl") or MINIMAX_BASE))
    body: dict[str, Any] = {
        "model": model.get("model"),
        "prompt": text,
        "response_format": "url",
        "n": max(1, min(9, int(count or 1))),
        "aigc_watermark": model.get("noWatermark") is False,
    }
    s = parse_size_or_ratio(size)
    if s.get("width"):
        body["width"] = s["width"]
        body["height"] = s["height"]
    elif s.get("aspectRatio"):
        body["aspect_ratio"] = s["aspectRatio"]
    if ref:
        body["subject_reference"] = [{"type": "character", "image_file": ref}]
    extra = read_extra_params(model, '{"prompt_optimizer":true,"seed":1234}')
    if extra:
        body.update(extra)

    parsed = await post_json(join_url(base, "/v1/image_generation"), model.get("apiKey") or "", body)
    items: list[dict] = []
    for u in ((parsed.get("data") or {}).get("image_urls") or []):
        if u:
            items.append({"url": str(u)})
    for b in ((parsed.get("data") or {}).get("image_base64") or []):
        if b:
            items.append({"b64": str(b)})
    if not items:
        detail = (
            ((parsed.get("base_resp") or {}).get("status_msg"))
            or ((parsed.get("error") or {}).get("message"))
            or json.dumps(parsed, ensure_ascii=False)[:300]
        )
        raise ImageGenError(f"海螺生图没有返回图片：{detail}", raw=parsed)
    return items


# ============================================================ Cloudflare


def shrink_cloudflare_ref(uri: str) -> dict[str, Any]:
    """FLUX.2 硬性要求参考图 < 512×512，本机图多半超限。

    原版借 Electron 的 nativeImage 等比缩到 512 以内；Python 版用 Pillow。
    已经合规、或 Pillow 解不开时原样返回。
    """
    info = split_data_uri(uri)
    try:
        buffer = base64.b64decode(info["base64"])
    except (base64.binascii.Error, ValueError):
        buffer = b""
    mime = info["mimeType"] or "image/png"
    ext = "jpg" if "jpeg" in mime else ("webp" if "webp" in mime else "png")
    if not buffer:
        return {"buffer": buffer, "mimeType": mime, "ext": ext}

    try:
        from PIL import Image

        with Image.open(BytesIO(buffer)) as img:
            width, height = img.size
            if width <= 512 and height <= 512:
                return {"buffer": buffer, "mimeType": mime, "ext": ext}
            scale = 512 / max(width, height)
            resized = img.convert("RGB").resize(
                (max(1, round(width * scale)), max(1, round(height * scale)),
                 ), Image.LANCZOS
            )
            out = BytesIO()
            resized.save(out, format="PNG")
            data = out.getvalue()
            if data:
                return {"buffer": data, "mimeType": "image/png", "ext": "png"}
    except Exception as err:  # noqa: BLE001
        log.warning("[imagegen] Cloudflare 参考图压缩失败，按原图发送：%s", err)
    return {"buffer": buffer, "mimeType": mime, "ext": ext}


async def generate_one_cloudflare(
    *, model: dict, prompt: str, size: str, count: int, refs: list[str]
) -> list[dict]:
    """Cloudflare Workers AI（FLUX.2）：Bearer Token + multipart/form-data，一次请求出 1 张。"""
    if not model.get("accountId"):
        raise ImageGenError(f"Cloudflare 模型「{model.get('name')}」还没有填写 Account ID")
    base = re.sub(r"/+$", "", str(model.get("baseUrl") or CLOUDFLARE_BASE))
    url = (
        f"{base}/client/v4/accounts/"
        f"{urllib.parse.quote(str(model.get('accountId')), safe='')}/ai/run/{model.get('model')}"
    )
    extra = read_extra_params(model, '{"guidance":3.5,"seed":12345}')
    wh = parse_wxh(size)
    times = max(1, min(MAX_COUNT, int(count or 1)))
    picked = (refs or [])[:CLOUDFLARE_MAX_REFS]
    prepared = [shrink_cloudflare_ref(uri) for uri in picked]

    all_items: list[dict] = []
    for _ in range(times):
        form: dict[str, Any] = {"prompt": prompt}
        if wh:
            form["width"] = str(wh["width"])
            form["height"] = str(wh["height"])
        files: list[tuple[str, tuple[str, bytes, str]]] = []
        for idx, item in enumerate(prepared):
            files.append((
                f"input_image_{idx}",
                (f"ref{idx}.{item['ext']}", item["buffer"], item["mimeType"]),
            ))
        # 逃生舱：额外的表单字段（guidance / seed 等），原样并入
        if extra:
            for key, value in extra.items():
                form[key] = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        all_items.extend(await post_cloudflare(url, model.get("apiKey") or "", form, files))
    return all_items


async def post_cloudflare(
    url: str, api_key: str, form: dict[str, Any], files: list[tuple[str, tuple[str, bytes, str]]]
) -> list[dict]:
    """发一次 Cloudflare multipart 请求；返回 [{url} | {b64}]"""
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S) as client:
        try:
            res = await client.post(
                url, headers={"authorization": f"Bearer {api_key}"}, data=form, files=files,
            )
        except httpx.TimeoutException as err:
            raise ImageGenError("请求超时：Cloudflare 接口长时间没有响应") from err
        except httpx.HTTPError as err:
            raise ImageGenError(f"无法连接 Cloudflare 接口：{err}") from err

    content_type = str(res.headers.get("content-type") or "")
    if res.status_code >= 400:
        raise ImageGenError(
            describe_cloudflare_error(res.status_code, res.text), code=res.status_code
        )
    # 有的模型直接回图片字节，有的回 JSON 里的 base64
    if re.match(r"^image/", content_type, re.I):
        buffer = res.content
        if not buffer:
            raise ImageGenError("Cloudflare 返回了空图片")
        return [{"b64": base64.b64encode(buffer).decode("ascii")}]

    text = res.text
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    items = extract_cloudflare_items(parsed)
    if not items:
        raise ImageGenError(f"Cloudflare 没有返回图片数据：{(text or '')[:300]}")
    return items


def extract_cloudflare_items(parsed: Any) -> list[dict]:
    """从 Cloudflare 响应里挖出图片：兼容 {result:{image}}、{image}、{result:'base64'}、{data:[...]}"""
    if not isinstance(parsed, (dict, list)):
        return []
    root = parsed.get("result") if isinstance(parsed, dict) and parsed.get("result") is not None else parsed
    nodes = root if isinstance(root, list) else [root]
    items: list[dict] = []
    for node in nodes:
        if not node:
            continue
        if isinstance(node, str):
            push_cloudflare_item(items, node)
            continue
        value = node.get("image") if isinstance(node, dict) else None
        if isinstance(node, dict):
            value = node.get("image") or node.get("images") or node.get("b64_json") or node.get("data")
        if isinstance(value, list):
            for entry in value:
                push_cloudflare_item(items, entry)
        elif value:
            push_cloudflare_item(items, value)
    return items


def push_cloudflare_item(items: list[dict], value: Any) -> None:
    """单个候选值：http 链接按 url 收，data URI / 裸 base64 一律转成 b64"""
    if not value:
        return
    if isinstance(value, dict):
        if value.get("url"):
            items.append({"url": str(value["url"])})
        elif value.get("image"):
            push_cloudflare_item(items, value["image"])
        elif value.get("b64_json"):
            items.append({"b64": str(value["b64_json"])})
        return
    text = str(value)
    if re.match(r"^https?:", text, re.I):
        items.append({"url": text})
    else:
        items.append({"b64": re.sub(r"^data:[^;]+;base64,", "", text)})


def describe_cloudflare_error(status: int, text: str) -> str:
    """Cloudflare 的错误体是 {success:false, errors:[{code,message}]}，抽出来翻成中文"""
    detail = str(text or "")
    try:
        parsed = json.loads(detail)
        msg = (
            ((parsed.get("errors") or [{}])[0] or {}).get("message")
            or (parsed.get("error") or {}).get("message")
            or parsed.get("message")
        )
        if msg:
            detail = msg
    except (json.JSONDecodeError, AttributeError, IndexError, TypeError):
        pass
    if status == 400 and re.search(r"image|shape|tensor|512", detail, re.I):
        return (
            "Cloudflare 拒绝了参考图（HTTP 400）：官方要求每张参考图小于 512×512、最多 4 张。"
            f"原始信息：{detail}"
        )
    if status == 401:
        return f"Cloudflare 鉴权失败（HTTP 401）：API Token 无效或已过期。原始信息：{detail}"
    if status == 403:
        return f"Cloudflare 权限不足（HTTP 403）：Token 需要勾选「Workers AI」读取权限。原始信息：{detail}"
    if status == 429:
        return (
            "Cloudflare 限流或免费额度用尽（HTTP 429）：免费档每天 10,000 Neurons，"
            f"00:00 UTC 重置。原始信息：{detail}"
        )
    return f"Cloudflare 接口返回 {status}：{detail}"


# ============================================================ 通用请求


def build_request_body(model: dict, *, prompt: str, size: str, count: int, refs: list[str]) -> dict:
    """组装请求体；模型配置里的额外参数最后合并，可覆盖任何默认字段"""
    n = max(1, min(MAX_COUNT, int(count or 1)))
    lst = (refs or [])[:MAX_REFS]
    protocol = model.get("protocol")
    body: dict[str, Any] = {"model": model.get("model"), "prompt": prompt}
    # 智谱接口没有 response_format 字段，只认 data:[{url}]，不下发
    if protocol != "zhipu":
        body["response_format"] = "url"

    if protocol == "ark":
        if size:
            body["size"] = size
        body["watermark"] = model.get("noWatermark") is False  # False = 不加水印
        # 参考图：单张用字符串、多张用数组，两种写法方舟都收
        if lst and model.get("supportsImageInput") is not False:
            body["image"] = lst[0] if len(lst) == 1 else lst
        # 只有要多张时才带组图参数：部分模型（如 5.0 pro）不支持该字段，传了会报错
        if n > 1:
            body["sequential_image_generation"] = "auto"
            body["max_images"] = n
    elif protocol == "zhipu":
        # 智谱只认「宽x高」枚举，填了枚举外的值会 400 拒收，干脆不下发
        if size and str(size) in ZHIPU_IMAGE_SIZES:
            body["size"] = str(size)
        # watermark_enabled 默认不加水印；关掉了才显式传 true
        if model.get("noWatermark") is False:
            body["watermark_enabled"] = True
    else:
        # openai 兼容协议：勾选了「支持图生图」时，把 base64 参考图放进 image 字段
        if model.get("supportsImageInput") and lst:
            body["image"] = lst[0] if len(lst) == 1 else lst
        if size:
            body["size"] = size
        body["n"] = n

    extra = read_extra_params(model)
    if extra:
        body.update(extra)
    return body


async def post_json(url: str, api_key: str, body: dict) -> dict:
    """发一个 Bearer 鉴权的 JSON POST，返回解析后的 JSON"""
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S) as client:
        try:
            res = await client.post(
                url,
                headers={"content-type": "application/json", "authorization": f"Bearer {api_key}"},
                content=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            )
        except httpx.TimeoutException as err:
            raise ImageGenError("请求超时：图片生成接口长时间没有响应") from err
        except httpx.HTTPError as err:
            raise ImageGenError(f"无法连接图片生成接口：{err}") from err

    text = res.text
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None

    if res.status_code >= 400:
        detail = (
            ((parsed or {}).get("error") or {}).get("message")
            or (parsed or {}).get("message")
            or (parsed or {}).get("msg")
            or text[:300]
            or f"HTTP {res.status_code}"
        )
        raise ImageGenError(
            describe_image_error(res.status_code, detail), code=res.status_code, raw=parsed
        )
    if parsed is None:
        raise ImageGenError(f"接口返回的不是 JSON：{text[:300]}")
    return parsed


def describe_image_error(status: int, detail: str) -> str:
    """把厂商返回的英文错误翻成用户看得懂的中文。

    重点是火山系（方舟 ark / 即梦）的内容审核 400。
    """
    text = str(detail or "")
    # 火山方舟欠费时也返回 403，原文是 "account has an overdue balance"。
    # 不先认出来的话会被下面的 403 分支译成「鉴权失败」，把人引到 API Key 上去白折腾。
    if re.search(r"overdue|欠费|余额不足|arrears|insufficient\s+balance", text, re.I):
        return f"账户已欠费：请求被拒（HTTP {status}），请到火山方舟控制台「费用中心」充值后再试。原始信息：{text}"
    if status == 400 and re.search(r"sensitive|moderation|risk|safety|nsfw|内容|审核", text, re.I):
        return (
            "参考图没通过内容审核，接口拒绝了这次请求。可以试试："
            "① 换一张参考图（截图边角、水印、人脸特写都容易被误判）；"
            "② 关掉参考图，改成纯文生图；"
            "③ 换一个审核更宽松的模型 / 服务商。"
        )
    if status in (401, 403):
        return f"鉴权失败（HTTP {status}）：API Key 可能无效、过期，或没有开通该模型。原始信息：{text}"
    if status == 429:
        return f"请求太频繁或额度用尽（HTTP 429），稍等一会儿再试。原始信息：{text}"
    return f"接口返回 {status}：{text}"


def extract_items(parsed: Any) -> list[dict]:
    """兼容解析 {data:[...]} / {images:[...]} 两种列表形态"""
    if not isinstance(parsed, dict):
        return []
    lst = parsed.get("data") if isinstance(parsed.get("data"), list) else (
        parsed.get("images") if isinstance(parsed.get("images"), list) else []
    )
    items: list[dict] = []
    for item in lst:
        if not item:
            continue
        if isinstance(item, str):
            items.append({"url": item})
        elif isinstance(item, dict):
            if item.get("url"):
                items.append({"url": str(item["url"])})
            elif item.get("b64_json"):
                items.append({"b64": str(item["b64_json"])})
    return items


def model_supports_image_input(model: dict) -> bool:
    """模型是否开启了图生图（ark 老数据没这个字段时按支持处理）"""
    # 智谱 CogView 接口没有参考图字段，勾了「支持图生图」也发不出去，一律按不支持算
    if model.get("protocol") == "zhipu":
        return False
    if model.get("supportsImageInput") is True:
        return True
    return model.get("protocol") == "ark" and model.get("supportsImageInput") is not False


def size_to_ratio(size: Any) -> str:
    """把前端传来的 size 归一成 '9:16' 这种比例。

    画布给豆包节点存的是比例标签，走普通图片模块时存的是具体尺寸 —— 两种都得认。
    """
    raw = str(size or "").strip()
    by_ratio = re.match(r"^(\d{1,2}):(\d{1,2})$", raw)
    if by_ratio:
        return f"{int(by_ratio.group(1))}:{int(by_ratio.group(2))}"
    by_px = re.match(r"^(\d{3,5})\s*[xX×]\s*(\d{3,5})$", raw)
    if not by_px:
        return ""
    a, b = int(by_px.group(1)), int(by_px.group(2))

    def _gcd(x: int, y: int) -> int:
        return x if y == 0 else _gcd(y, x % y)

    g = _gcd(a, b) or 1
    a, b = a // g, b // g
    # 约不到常见档位就不硬猜（例如 1023x1025），交给豆包自己的默认画幅
    return f"{a}:{b}" if a <= 32 and b <= 32 else ""


# ============================================================ 主入口


async def generate_images(
    *,
    model: dict,
    prompt: str,
    size: str,
    count: int,
    refs: list[dict] | None = None,
    on_progress=None,
) -> list[dict]:
    """提交一次生成请求，返回 [{url} | {b64}]。

    refs 是任务上的参考图快照（本机文件带 localPath，链接图带 url）。
    对应 Node 版 `export async function generateImages(...)`。
    """
    # 本地豆包桥不需要模型 ID 也不需要 API Key：它驱动的是本机那个豆包客户端
    is_doubao_bridge = model.get("protocol") == "doubao"
    if not is_doubao_bridge:
        if not model.get("model"):
            raise ImageGenError("这个图片模型还没有填写模型 ID")
        if not model.get("apiKey"):
            raise ImageGenError(f"图片模型「{model.get('name')}」还没有填写 API Key")

    snapshots = refs or []
    url_refs = [r for r in snapshots if is_url_ref(r)]
    file_refs = [r for r in snapshots if not is_url_ref(r)]
    if snapshots:
        if not model_supports_image_input(model):
            raise ImageGenError(
                f"智谱 CogView 系列（当前「{model.get('model')}」）接口没有参考图字段，做不到图生图 —— 参考图发不出去。"
                "要「照参考图画」，请换成支持参考图的模型：通义万相 wan2.7-image-pro / 腾讯混元 hy-image-v3.5-preview / 即梦 jimeng_i2i_v30。"
                if model.get("protocol") == "zhipu"
                else f"图片模型「{model.get('name')}」未开启图生图，请在模型管理里勾选「支持图生图」或换一个模型"
            )
        if len(snapshots) > MAX_REFS:
            raise ImageGenError(f"参考图最多 {MAX_REFS} 张，当前 {len(snapshots)} 张")

    # -------------------------------------------------- 本机豆包桥
    if is_doubao_bridge:
        if url_refs:
            raise ImageGenError(
                "本地豆包桥的参考图只认本机文件，链接图请先下载后再以「上传参考图」方式添加"
            )
        files = [r.get("localPath") for r in file_refs if r.get("localPath") and os.path.exists(r["localPath"])]
        if file_refs and not files:
            raise ImageGenError("参考图文件已被删除，请重新添加参考图")
        from .doubao_bridge import generate_with_doubao

        return await generate_with_doubao(
            prompt=prompt,
            ref_files=files,
            count=count,
            ratio=size_to_ratio(size),
            endpoint=model.get("endpoint") or model.get("baseUrl") or "",
            on_progress=on_progress,
        )

    # -------------------------------------------------- 即梦
    if model.get("protocol") == "jimeng":
        if not model.get("akSecret"):
            raise ImageGenError(f"即梦模型「{model.get('name')}」还没有填写 AccessKeySecret")
        ref_urls = [r.get("url") for r in url_refs]
        ref_b64 = read_refs_data_uris(file_refs[:1]) if file_refs else []
        if file_refs and not ref_b64:
            raise ImageGenError("参考图文件已被删除，请重新添加参考图")
        times = max(1, min(MAX_COUNT, int(count or 1)))
        out: list[dict] = []
        for _ in range(times):
            out.extend(await generate_one_jimeng(
                model=model, prompt=prompt, size=size, ref_urls=ref_urls, ref_base64=ref_b64,
            ))
        return out

    # -------------------------------------------------- 通义万相
    if model.get("protocol") == "wanxiang":
        model_id = str(model.get("model") or "")
        is_t2i = any(model_id.startswith(p) for p in WANXIANG_T2I_PREFIXES)
        if not snapshots:
            if not is_t2i:
                raise ImageGenError(
                    "通义万相这个模型需要至少 1 张参考图（请切到图生图模式并添加参考图），"
                    "或把模型换成文生图模型（如 wan2.2-t2i-flash）"
                )
            return await generate_one_wanxiang_t2i(model=model, prompt=prompt, size=size, count=count)
        if is_t2i:
            raise ImageGenError(
                "这个通义万相模型是文生图专用，请去掉参考图，或换成 wan2.6-image 这类图生图模型"
            )
        if any(model_id.startswith(p) for p in WANXIANG_MM_PREFIXES):
            mm_refs = [r.get("url") for r in url_refs if r.get("url")]
            file_uris = read_refs_data_uris(file_refs)
            if file_refs and not file_uris:
                raise ImageGenError("参考图文件已被删除，请重新添加参考图")
            mm_refs.extend(file_uris)
            return await generate_one_wanxiang_mm(
                model=model, prompt=prompt, size=size, count=count, refs=mm_refs
            )
        first = snapshots[0]
        if is_url_ref(first):
            base_image = first.get("url") or ""
        else:
            data_uris = read_refs_data_uris([first])
            if not data_uris:
                raise ImageGenError("参考图文件已被删除，请重新添加参考图")
            base_image = data_uris[0]
        return await generate_one_wanxiang(
            model=model, prompt=prompt, count=count, base_image=base_image
        )

    # -------------------------------------------------- 腾讯混元
    if model.get("protocol") == "hunyuan":
        lst = [r.get("url") for r in url_refs]
        file_uris = read_refs_data_uris(file_refs[:3])
        if file_refs and not file_uris:
            raise ImageGenError("参考图文件已被删除，请重新添加参考图")
        lst.extend(file_uris)
        return await generate_one_hunyuan(model=model, prompt=prompt, size=size, refs=lst[:3])

    # -------------------------------------------------- MiniMax 海螺
    if model.get("protocol") == "minimax":
        first = snapshots[0] if snapshots else None
        ref = None
        if first:
            if is_url_ref(first):
                ref = first.get("url")
            else:
                data_uris = read_refs_data_uris([first])
                if not data_uris:
                    raise ImageGenError("参考图文件已被删除，请重新添加参考图")
                ref = data_uris[0]
        return await generate_one_minimax(
            model=model, prompt=prompt, size=size, count=count, ref=ref
        )

    # -------------------------------------------------- Cloudflare
    if model.get("protocol") == "cloudflare":
        if url_refs:
            raise ImageGenError(
                "Cloudflare 的参考图走本机文件的字节流，链接图请先下载后再以「上传参考图」方式添加"
            )
        data_uris = read_refs_data_uris(file_refs)
        if file_refs and not data_uris:
            raise ImageGenError("参考图文件已被删除，请重新添加参考图")
        return await generate_one_cloudflare(
            model=model, prompt=prompt, size=size, count=count, refs=data_uris
        )

    # -------------------------------------------------- 智谱（无参考图字段）
    if model.get("protocol") == "zhipu" and snapshots:
        raise ImageGenError("智谱 CogView 接口不支持参考图，没法图生图，请换方舟或兼容协议的模型")

    # -------------------------------------------------- OpenAI 兼容 / 方舟
    if url_refs:
        raise ImageGenError("这个协议的参考图走本机文件 base64，链接图目前只有即梦和通义万相支持")
    refs_data = read_refs_data_uris(file_refs)
    if file_refs and not refs_data:
        raise ImageGenError("参考图文件已被删除，请重新添加参考图")

    url = join_url(model.get("baseUrl"), "/images/generations")
    # 智谱接口没有「一次出多张」的参数，要多张就串行请求几次
    times = max(1, min(MAX_COUNT, int(count or 1))) if model.get("protocol") == "zhipu" else 1
    all_items: list[dict] = []
    for _ in range(times):
        body = build_request_body(
            model, prompt=prompt, size=size,
            count=1 if times > 1 else count, refs=refs_data,
        )
        parsed = await post_json(url, model.get("apiKey") or "", body)
        items = extract_items(parsed)
        if not items:
            raise ImageGenError(
                f"接口没有返回图片数据：{json.dumps(parsed, ensure_ascii=False)[:300]}"
            )
        all_items.extend(items)
    return all_items


# ============================================================ 结果落地


def guess_extension(url: str, content_type: str | None) -> str:
    """按 Content-Type 优先、URL 后缀兜底，猜一个图片扩展名"""
    ctype = str(content_type or "").lower()
    if "jpeg" in ctype:
        return ".jpg"
    if "webp" in ctype:
        return ".webp"
    if "png" in ctype:
        return ".png"
    try:
        ext = os.path.splitext(urllib.parse.urlparse(url).path)[1].lower()
        if ext in (".jpg", ".jpeg", ".png", ".webp"):
            return ".jpg" if ext == ".jpeg" else ext
    except ValueError:
        pass
    return ".png"


async def download_image(item: dict) -> tuple[bytes, str]:
    """把生成结果拉回本地，返回 (buffer, ext)。

    带 mimeType 的（本地豆包桥抓回来的可能是 jpeg/webp）按真实类型命名，
    否则一律 .png —— 扩展名错了浏览器照样能显示，但下载/转视频那条链路会按后缀判断。
    """
    if item.get("b64"):
        try:
            buffer = base64.b64decode(item["b64"])
        except (base64.binascii.Error, ValueError) as err:
            raise ImageGenError(f"下载图片失败：base64 解码失败 {err}") from err
        return buffer, guess_extension("", item.get("mimeType"))

    async with httpx.AsyncClient(timeout=DOWNLOAD_TIMEOUT_S, follow_redirects=True) as client:
        try:
            res = await client.get(item.get("url") or "")
        except httpx.TimeoutException as err:
            raise ImageGenError("下载图片超时") from err
        except httpx.HTTPError as err:
            raise ImageGenError(f"下载图片失败：{err}") from err

    if res.status_code >= 400:
        raise ImageGenError(f"下载图片失败：HTTP {res.status_code}")
    buffer = res.content
    if not buffer:
        raise ImageGenError("下载到的内容为空")
    return buffer, guess_extension(item.get("url") or "", res.headers.get("content-type"))
