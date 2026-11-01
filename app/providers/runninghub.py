"""RunningHub OpenAPI 适配器 —— 逐函数复刻 `server/providers/runninghub.js`。

流程：上传素材 → 提交任务 → 轮询结果
  POST /task/openapi/upload        multipart: apiKey / fileType / file
  GET  /api/webapp/apiCallDemo     读取 AI 应用的可填参数（nodeInfoList）
  POST /task/openapi/ai-app/run    发起 AI 应用任务
  POST /task/openapi/create        发起 ComfyUI 工作流任务
  POST /task/openapi/outputs       查询结果 / 状态
  POST /uc/openapi/accountStatus   查询账户信息（RH 币余额）
  POST /task/openapi/cancel        取消任务

站点：国内站 runninghub.cn / 国际站 runninghub.ai，账号上存 site，请求按站点拼域名。
国际站走系统代理（httpx 的 trust_env=True 会自动读 HTTP(S)_PROXY / 系统代理），
失败再回落直连一次。
"""

from __future__ import annotations

import json
import logging
import re
import urllib.parse
from typing import Any

import httpx

log = logging.getLogger(__name__)

DEFAULT_BASE = "https://www.runninghub.cn"

#: RunningHub 有国内站（.cn）和国际站（.ai）两套独立账号，Key 只在注册的那个站有效
SITE_BASES = {"cn": DEFAULT_BASE, "ai": "https://www.runninghub.ai"}

FILE_TYPES = {"IMAGE", "VIDEO", "AUDIO"}

DEFAULT_TIMEOUT_S = 120.0
ACCOUNT_TIMEOUT_S = 30.0
UPLOAD_TIMEOUT_S = 600.0


def base_for_site(site: str | None) -> str:
    return SITE_BASES.get(str(site or ""), DEFAULT_BASE)


class RunningHubError(Exception):
    """RunningHub 调用失败；带 code / raw 两个附加字段"""

    def __init__(self, message: str, *, code: Any = None, raw: Any = None) -> None:
        super().__init__(message)
        self.name = "RunningHubError"
        self.code = code
        self.raw = raw


def normalize_base(base_url: str | None) -> str:
    value = str(base_url or DEFAULT_BASE).strip().rstrip("/")
    return value or DEFAULT_BASE


def _hostname_of(url: str) -> str:
    try:
        return urllib.parse.urlparse(url).hostname or ""
    except ValueError:
        return ""


async def request(
    base_url: str | None,
    pathname: str,
    *,
    method: str = "POST",
    api_key: str | None = None,
    body: Any = None,
    files: dict | None = None,
    data: dict | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> dict:
    """发一个 OpenAPI 请求并解析 JSON。

    files 非空走 multipart；否则 body 走 JSON。
    国际站（.ai）先走代理（httpx trust_env 自动读系统代理），失败再回落直连一次。
    """
    url = f"{normalize_base(base_url)}{pathname}"
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    kwargs: dict[str, Any] = {"headers": headers}
    if files:
        kwargs["files"] = files
        if data:
            kwargs["data"] = data
    elif body is not None:
        headers["Content-Type"] = "application/json"
        kwargs["content"] = json.dumps(body, ensure_ascii=False).encode("utf-8")

    hostname = _hostname_of(url)
    proxy_err: Exception | None = None
    res: httpx.Response | None = None
    try:
        async with httpx.AsyncClient(
            timeout=timeout_s, trust_env=True, follow_redirects=True
        ) as client:
            if hostname.endswith(".ai"):
                try:
                    res = await client.request(method, url, **kwargs)
                except httpx.HTTPError as err:
                    proxy_err = err
            if res is None:
                async with httpx.AsyncClient(
                    timeout=timeout_s, trust_env=False, follow_redirects=True
                ) as direct:
                    res = await direct.request(method, url, **kwargs)
    except httpx.TimeoutException as err:
        reason = (
            f"请求超时（{int(timeout_s * 1000)}ms）"
            if proxy_err is None
            else f"{proxy_err} → 直连也失败: {err}"
        )
        raise RunningHubError(
            f"请求 {pathname} 失败：{reason}"
            + (
                "。国际站需要网络畅通：请确认代理软件开着，或把该账号切回国内站"
                if "/openapi/" in pathname and hostname.endswith(".ai")
                else ""
            )
        ) from err
    except httpx.HTTPError as err:
        reason = f"{proxy_err} → 直连也失败: {err}" if proxy_err else str(err)
        raise RunningHubError(
            f"请求 {pathname} 失败：{reason}"
            + (
                "。国际站需要网络畅通：请确认代理软件开着，或把该账号切回国内站"
                if "/openapi/" in pathname and hostname.endswith(".ai")
                else ""
            )
        ) from err

    text = res.text
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as err:
        raise RunningHubError(
            f"接口返回非 JSON（HTTP {res.status_code}）：{text[:200]}"
        ) from err
    if not isinstance(parsed, dict):
        raise RunningHubError(f"接口返回内容异常：{text[:200]}")
    return parsed


# ============================================================ 参数解析


def parse_field_data(field_data: Any) -> list[dict[str, str]]:
    """把 fieldData 解析成 [{index, label}]。

    ComfyUI 的控件定义形如 ["BOOLEAN", {"default": false}] / ["FLOAT", {...}]，
    这是「类型 + 约束」而不是可选项列表，按类型给出真正的可选值。
    """
    if not field_data:
        return []
    raw = field_data if isinstance(field_data, str) else json.dumps(field_data, ensure_ascii=False)
    try:
        arr = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(arr, list):
        return []

    # ["BOOLEAN", {...}] 形态：类型 + 约束
    if (
        len(arr) >= 2
        and isinstance(arr[0], str)
        and isinstance(arr[1], dict)
    ):
        kind = arr[0].upper()
        if kind in ("BOOLEAN", "BOOL"):
            return [
                {"index": "true", "label": "true（开启）"},
                {"index": "false", "label": "false（关闭）"},
            ]
        return []

    out: list[dict[str, str]] = []
    for item in arr:
        if item is None:
            continue
        if isinstance(item, (str, int, float)) and not isinstance(item, bool):
            out.append({"index": str(item), "label": str(item)})
            continue
        if isinstance(item, dict):
            index = item.get("index") or item.get("name") or item.get("value") or str(item)
            label = item.get("description") or item.get("name") or str(index)
            out.append({"index": str(index), "label": str(label)})
    return out


def infer_field_type(item: dict) -> str:
    """按 fieldType 归一，没有就按字段名猜"""
    raw = str(item.get("fieldType") or "").strip().upper()
    if raw:
        if raw in FILE_TYPES:
            return raw
        if raw in ("LIST", "COMBO", "ENUM", "SELECT"):
            return "LIST"
        if raw in ("NUMBER", "INT", "INTEGER", "FLOAT", "DOUBLE"):
            return "NUMBER"
        if raw in ("STRING", "TEXT", "BOOLEAN", "BOOL"):
            return "LIST" if raw in ("BOOLEAN", "BOOL") else "STRING"
        return raw
    if item.get("fieldData"):
        return "LIST"
    name = str(item.get("fieldName") or "").lower()
    if "video" in name:
        return "VIDEO"
    if "audio" in name:
        return "AUDIO"
    if "image" in name or "mask" in name:
        return "IMAGE"
    return "STRING"


def normalize_field(item: dict) -> dict:
    """平台原始字段 → 前端使用的字段快照"""
    field_type = infer_field_type(item)
    options = parse_field_data(item.get("fieldData")) if field_type == "LIST" else []
    return {
        "nodeId": str(item.get("nodeId") if item.get("nodeId") is not None else ""),
        "nodeName": str(item.get("nodeName") or ""),
        "fieldName": str(item.get("fieldName") or ""),
        "fieldType": field_type,
        "description": str(item.get("description") or item.get("descriptionEn") or ""),
        "defaultValue": "" if item.get("fieldValue") is None else str(item.get("fieldValue")),
        "options": options,
    }


# ============================================================ 接口调用


async def inspect_app(*, base_url: str | None, api_key: str, app_id: str) -> dict:
    """读取 AI 应用的可填参数，返回 {appName, instanceType, fields}"""
    query = urllib.parse.urlencode({"apiKey": api_key, "webappId": str(app_id)})
    payload = await request(
        base_url, f"/api/webapp/apiCallDemo?{query}", method="GET", api_key=api_key
    )
    if payload.get("code") != 0:
        raise RunningHubError(
            payload.get("msg") or f"读取应用参数失败（code={payload.get('code')}）",
            code=payload.get("code"), raw=payload,
        )
    data = payload.get("data") or {}
    items = data.get("nodeInfoList") or []
    # 官方 curl 示例里带 instanceType（plus = 48G / ultra = 84G），不带上会落到 24G 标准实例
    match = re.search(r'"instanceType"\s*:\s*"([^"]+)"', str(data.get("curl") or ""))
    return {
        "appName": str(data.get("webappName") or ""),
        "instanceType": match.group(1) if match else "",
        "fields": [normalize_field(item) for item in items if isinstance(item, dict)],
    }


async def fetch_account_status(*, base_url: str | None, api_key: str) -> dict:
    """查询账户信息：RH 币余额、钱包金额、当前任务数"""
    payload = await request(
        base_url, "/uc/openapi/accountStatus", api_key=api_key,
        # 文档示例里是小写 apikey，别的接口都是 apiKey，两个都带上最稳
        body={"apiKey": api_key, "apikey": api_key},
        timeout_s=ACCOUNT_TIMEOUT_S,
    )
    if payload.get("code") != 0:
        raise RunningHubError(
            payload.get("msg") or f"查询账户信息失败（code={payload.get('code')}）",
            code=payload.get("code"), raw=payload,
        )

    def num(value: Any) -> float | int | None:
        if value is None or value == "":
            return None
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return int(parsed) if parsed.is_integer() else parsed

    data = payload.get("data") or {}
    return {
        "remainCoins": num(data.get("remainCoins")),
        "remainMoney": num(data.get("remainMoney")),
        "currency": str(data.get("currency") or ""),
        "apiType": str(data.get("apiType") or ""),
        "currentTaskCounts": num(data.get("currentTaskCounts")),
    }


async def upload_file(
    *,
    base_url: str | None,
    api_key: str,
    buffer: bytes,
    filename: str,
    mime_type: str | None = None,
    file_type: str = "input",
) -> str:
    """上传本地素材，返回平台侧文件名（可直接作为 fieldValue）"""
    files = {"file": (filename, buffer, mime_type or "application/octet-stream")}
    data = {"apiKey": api_key, "fileType": file_type}
    payload = await request(
        base_url, "/task/openapi/upload", method="POST", api_key=api_key,
        files=files, data=data, timeout_s=UPLOAD_TIMEOUT_S,
    )
    if payload.get("code") != 0:
        raise RunningHubError(
            payload.get("msg") or f"素材上传失败（code={payload.get('code')}）",
            code=payload.get("code"), raw=payload,
        )
    raw = payload.get("data")
    name = (
        (raw or {}).get("fileName") if isinstance(raw, dict) else None
    )
    if not name and isinstance(raw, dict):
        name = raw.get("fileNameWithPath") or raw.get("filePath")
    if not name and isinstance(raw, str):
        name = raw
    if not name:
        raise RunningHubError(
            f"素材上传返回缺少文件名：{json.dumps(payload, ensure_ascii=False)[:200]}", raw=payload
        )
    return str(name)


def assert_create_ok(payload: dict, label: str) -> dict:
    """校验创建任务的返回：code 必须 0，且必须有 taskId"""
    if payload.get("code") != 0:
        raise RunningHubError(
            payload.get("msg") or f"{label}失败（code={payload.get('code')}）",
            code=payload.get("code"), raw=payload,
        )
    task_id = (payload.get("data") or {}).get("taskId")
    if not task_id:
        raise RunningHubError(
            f"{label}成功但未返回 taskId：{json.dumps(payload, ensure_ascii=False)[:200]}", raw=payload
        )
    return payload


async def create_ai_app_task(
    *,
    base_url: str | None,
    api_key: str,
    app_id: str,
    node_info_list: list[dict],
    instance_type: str | None = None,
) -> dict:
    body: dict[str, Any] = {
        "apiKey": api_key,
        "webappId": str(app_id),
    }
    if instance_type:
        body["instanceType"] = instance_type
    body["nodeInfoList"] = node_info_list
    payload = await request(
        base_url, "/task/openapi/ai-app/run", api_key=api_key, body=body
    )
    return assert_create_ok(payload, "发起 AI 应用任务")


async def create_workflow_task(
    *,
    base_url: str | None,
    api_key: str,
    workflow_id: str,
    node_info_list: list[dict],
    instance_type: str | None = None,
) -> dict:
    body: dict[str, Any] = {
        "apiKey": api_key,
        "workflowId": str(workflow_id),
    }
    if instance_type:
        body["instanceType"] = instance_type
    body["nodeInfoList"] = node_info_list
    payload = await request(base_url, "/task/openapi/create", api_key=api_key, body=body)
    return assert_create_ok(payload, "发起工作流任务")


def parse_prompt_tips(prompt_tips: Any) -> dict | None:
    if not prompt_tips:
        return None
    if isinstance(prompt_tips, dict):
        return prompt_tips
    try:
        parsed = json.loads(prompt_tips)
        return parsed if isinstance(parsed, dict) else None
    except (json.JSONDecodeError, TypeError):
        return None


async def query_outputs(*, base_url: str | None, api_key: str, task_id: str) -> dict:
    """查询任务结果，返回 {state, outputs?, error?, raw}

    code: 0 成功 / 804 运行中 / 813 排队中 / 805 失败
    """
    payload = await request(
        base_url, "/task/openapi/outputs", api_key=api_key,
        body={"apiKey": api_key, "taskId": str(task_id)},
    )
    code = payload.get("code")

    if code == 0:
        data = payload.get("data")
        if isinstance(data, list):
            if not data:
                return {"state": "running", "raw": payload}
            outputs = []
            for item in data:
                if not isinstance(item, dict):
                    continue
                file_url = item.get("fileUrl") or item.get("file_url") or ""
                if not file_url:
                    continue
                outputs.append({
                    "fileUrl": file_url,
                    "fileType": str(item.get("fileType") or "").lower(),
                    "nodeId": str(item.get("nodeId")) if item.get("nodeId") else "",
                    "costTime": item.get("taskCostTime"),
                })
            if not outputs:
                return {"state": "running", "raw": payload}
            return {"state": "success", "outputs": outputs, "raw": payload}
        return {"state": "running", "raw": payload}
    if code == 804:
        return {"state": "running", "raw": payload}
    if code == 813:
        return {"state": "queued", "raw": payload}
    if code == 805:
        reason = (payload.get("data") or {}).get("failedReason") or {}
        if isinstance(reason, dict):
            detail = " · ".join(
                str(x) for x in (reason.get("node_name"), reason.get("exception_message")) if x
            )
        else:
            detail = str(reason or "")
        return {
            "state": "failed",
            "error": detail or payload.get("msg") or "任务执行失败",
            "raw": payload,
        }
    return {
        "state": "failed",
        "error": f"{payload.get('msg') or '未知错误'}（code={code}）",
        "raw": payload,
    }


async def cancel_task(*, base_url: str | None, api_key: str, task_id: str) -> dict:
    return await request(
        base_url, "/task/openapi/cancel", api_key=api_key,
        body={"apiKey": api_key, "taskId": str(task_id)},
        timeout_s=ACCOUNT_TIMEOUT_S,
    )
