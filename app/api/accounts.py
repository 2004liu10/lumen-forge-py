"""账号池 —— RunningHub API Key 的增删改查、测试、余额。

对应 `server/index.js:403-682`。账号是画布任务的执行主体：每个账号有自己的
站点（国内 / 国际）、并发上限、每日额度，调度器按这些挑人。
"""

from __future__ import annotations

import asyncio
import logging
import math
import re

from fastapi import APIRouter, Request

from ..models import new_account
from ..providers import runninghub as rh
from ..scheduler import account_snapshot, mask_key, refresh_account_balance
from ..store import store
from .deps import as_int, bad_request, body_str, fail, json_body, not_found, ok, trim

log = logging.getLogger(__name__)

router = APIRouter()

# 平台把「key 无效」放在 msg 里回，没有错误码可依，只能按文案认
_INVALID_KEY_RE = re.compile(r"api\s*key|apikey|key\s*无效|invalid", re.I)


def to_daily_limit(value, fallback: int) -> int:
    """`toDailyLimit(v, fallback)` —— 非法或负数都退回落值，否则向下取整"""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return fallback
    if not math.isfinite(num) or num < 0:
        return fallback
    return int(math.floor(num))


def find_account(account_id: str) -> dict | None:
    for account in store.accounts:
        if account.get("id") == account_id:
            return account
    return None


@router.post("/api/accounts")
async def create_account(request: Request) -> dict:
    body = await json_body(request)
    name = trim(body.get("name"))
    api_key = trim(body.get("apiKey"))
    if not name:
        bad_request("请填写账号名称")
    if not api_key:
        bad_request("请填写 API Key")

    account = new_account(name, api_key)
    if body.get("site") in ("ai", "cn"):
        account["site"] = body["site"]
    account["concurrency"] = max(1, as_int(body.get("concurrency"), 1))
    if body.get("remark") is not None:
        account["remark"] = trim(body.get("remark"))
    account["dailyLimit"] = to_daily_limit(body.get("dailyLimit"), 1)
    if body.get("loginName") is not None:
        account["loginName"] = trim(body.get("loginName"))
    if body.get("loginPass") is not None:
        account["loginPass"] = str(body.get("loginPass"))

    store.accounts.append(account)
    store.mark_dirty("accounts")
    store.save()
    store.flush()
    # 余额刷新是「顺手试一下」：失败只写进账号的 balance.error，不能挡住建账号
    asyncio.create_task(_safe_refresh(account))
    return account_snapshot(account)


async def _safe_refresh(account: dict) -> None:
    try:
        await refresh_account_balance(account, force=True)
    except Exception as err:  # noqa: BLE001
        log.warning("[accounts] 刷余额失败：%s", err)


@router.patch("/api/accounts/{account_id}")
async def patch_account(account_id: str, request: Request) -> dict:
    account = find_account(account_id)
    if not account:
        not_found("账号不存在")
    body = await json_body(request)

    if body.get("name") is not None and trim(body.get("name")):
        account["name"] = trim(body.get("name"))
    if body.get("apiKey") is not None and trim(body.get("apiKey")):
        account["apiKey"] = trim(body.get("apiKey"))
        account["balance"] = None
        asyncio.create_task(_safe_refresh(account))
    if body.get("site") in ("ai", "cn") and body["site"] != account.get("site"):
        account["site"] = body["site"]
        account["balance"] = None
        asyncio.create_task(_safe_refresh(account))
    if "concurrency" in body:
        account["concurrency"] = max(1, as_int(body.get("concurrency"), 1))
    if "enabled" in body:
        account["enabled"] = bool(body.get("enabled"))
    if "dailyLimit" in body:
        account["dailyLimit"] = to_daily_limit(body.get("dailyLimit"), int(account.get("dailyLimit") or 1))
    if "remark" in body:
        account["remark"] = trim(body.get("remark"))
    if "loginName" in body:
        account["loginName"] = trim(body.get("loginName"))
    if "loginPass" in body:
        account["loginPass"] = str(body.get("loginPass") or "")
    for key in ("loginAt", "loginExpiresAt"):
        if key in body:
            account[key] = as_int(body.get(key), 0)
    if "claimAt" in body:
        account["claimAt"] = as_int(body.get("claimAt"), 0)
    if "claimGain" in body:
        account["claimGain"] = as_int(body.get("claimGain"), 0)
    if "claimNote" in body:
        account["claimNote"] = trim(body.get("claimNote"))

    store.mark_dirty("accounts")
    store.save()
    store.flush()
    return account_snapshot(account)


@router.get("/api/accounts/{account_id}/credentials")
async def get_credentials(account_id: str) -> dict:
    account = find_account(account_id)
    if not account:
        not_found("账号不存在")
    return {
        "apiKey": account.get("apiKey") or "",
        "loginName": account.get("loginName") or "",
        "loginPass": account.get("loginPass") or "",
    }


@router.delete("/api/accounts/{account_id}")
async def delete_account(account_id: str) -> dict:
    account = find_account(account_id)
    if not account:
        not_found("账号不存在")
    store.accounts = [a for a in store.accounts if a.get("id") != account_id]
    # 画布绑的是这个账号，账号没了画布必须解绑，否则下次跑会派给不存在的账号
    for canvas in store.canvases:
        if canvas.get("accountId") == account_id:
            canvas["accountId"] = None
    store.mark_dirty("accounts")
    store.mark_dirty("canvases")
    store.save()
    store.flush()
    return ok()


@router.post("/api/accounts/{account_id}/test")
async def test_account(account_id: str) -> dict:
    """拿一个不存在的 taskId 去查输出：平台若回「key 无效」就是坏 key，否则就是通"""
    account = find_account(account_id)
    if not account:
        not_found("账号不存在")
    try:
        result = await rh.query_outputs(
            base_url=rh.base_for_site(account.get("site")),
            api_key=account.get("apiKey") or "",
            task_id="0",
        )
        message = ((result.get("raw") or {}).get("msg")) or ""
        invalid = bool(
            _INVALID_KEY_RE.search(message)
            or (result.get("raw") or {}).get("code") == 1002
        )
        return {
            "ok": not invalid,
            "level": "error" if invalid else "ok",
            "message": f"密钥无效：{message}" if invalid else f"密钥可用（平台返回：{message or '无'}）",
        }
    except Exception as err:  # noqa: BLE001 - 测连通性，失败本身就是结果
        return {"ok": False, "level": "error", "message": f"连接失败：{err}"}


@router.post("/api/accounts/refresh-balance")
async def refresh_all_balances() -> dict:
    """批量刷余额。**必须挂在 `/api/accounts/{id}` 之前**，否则会被当成 id=refresh-balance"""
    targets = [a for a in store.accounts if a.get("apiKey")]
    results = await asyncio.gather(
        *[_balance_entry(a) for a in targets], return_exceptions=True
    )
    out = []
    for entry in results:
        if isinstance(entry, Exception):
            out.append({"id": "", "name": "", "remainCoins": None, "error": str(entry)})
        else:
            out.append(entry)
    store.save()
    store.flush()
    return {"results": out}


async def _balance_entry(account: dict) -> dict:
    try:
        balance = await refresh_account_balance(account, force=True)
    except Exception as err:  # noqa: BLE001
        balance = {"error": str(err)}
    return {
        "id": account.get("id"),
        "name": account.get("name"),
        "remainCoins": (balance or {}).get("remainCoins"),
        "error": (balance or {}).get("error") or "",
    }


@router.post("/api/accounts/{account_id}/balance")
async def refresh_one_balance(account_id: str) -> dict:
    account = find_account(account_id)
    if not account:
        not_found("账号不存在")
    if not account.get("apiKey"):
        bad_request("这个账号还没有填 API Key")
    balance = await refresh_account_balance(account, force=True)
    return {"balance": balance, "account": account_snapshot(account)}


__all__ = ["router", "find_account", "to_daily_limit"]
