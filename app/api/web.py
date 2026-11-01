"""网页平台与网页账号 —— 对应 `server/index.js:503-627`。

这里管的是「用浏览器登录网页版 AI（豆包 / 千问 / 元宝…）来生成」的那一套账号。
跟 RunningHub 的 API Key 账号池完全独立：那边按并发派单，这边按每日额度轮换。
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from ..web_accounts import (
    commit_web_usage,
    fail_web_usage,
    find_web_account,
    new_web_account,
    pick_web_account,
    quota_left,
    web_account_snapshot,
)
from ..web_providers import (
    delete_provider,
    find_provider,
    list_providers,
    partition_of,
    reset_providers,
    upsert_provider,
)
from ..store import store
from .deps import as_int, bad_request, body_str, json_body, not_found, ok, str_or, trim

router = APIRouter()


# ------------------------------------------------------------------ 平台


@router.get("/api/web-providers")
async def get_providers() -> dict:
    return {"providers": list_providers()}


@router.post("/api/web-providers")
async def post_provider(request: Request) -> dict:
    body = await json_body(request)
    try:
        out = upsert_provider(body)
    except ValueError as err:
        bad_request(str(err))
    provider = out["provider"]
    return {
        "provider": {**provider, "partition": partition_of(provider.get("slug"))},
        "created": bool(out.get("created")),
    }


@router.delete("/api/web-providers/{provider_id}")
async def delete_web_provider(provider_id: str) -> dict:
    if not delete_provider(provider_id):
        not_found("平台不存在")
    return ok()


@router.post("/api/web-providers/reset")
async def reset_web_providers() -> dict:
    added = reset_providers()
    return {"added": added, "providers": list_providers()}


# ------------------------------------------------------------------ 账号


def _quota_of(slug: str) -> dict | None:
    provider = find_provider(slug)
    return (provider or {}).get("quota")


def _snapshot(account: dict) -> dict:
    return web_account_snapshot(account, _quota_of(account.get("provider") or ""))


def _pick_reason(provider_slug: str, provider_quota: dict | None) -> str:
    """挑不到账号时给用户一句能动手改的话（原版 `pickReason`）"""
    pool = [a for a in store.web_accounts if a.get("provider") == provider_slug]
    if not pool:
        return "这个平台还没绑账号"
    if not any(a.get("enabled") is not False for a in pool):
        return "这个平台的账号都被停用了"
    # 剩下「启用了、但额度用光」的账号：额度是有限值才算用光
    remaining = 0.0
    limited = False
    for account in pool:
        if account.get("enabled") is False:
            continue
        left = quota_left(account, provider_quota)
        if left is None:
            continue
        limited = True
        remaining += left
    if limited and remaining > 0:
        return "账号都不可用（登录态可能过期了）"
    return "今天所有账号的额度都用完了，明早 0 点会重置"


@router.get("/api/web-accounts")
async def list_web_accounts() -> dict:
    return {"accounts": [_snapshot(a) for a in store.web_accounts]}


@router.post("/api/web-accounts")
async def create_web_account(request: Request) -> dict:
    body = await json_body(request)
    provider_slug = trim(body.get("provider"))
    if not provider_slug:
        bad_request("请先选一个平台")
    if not find_provider(provider_slug):
        bad_request(f"没有这个平台：{provider_slug}")
    name = trim(body.get("name"))
    if not name:
        bad_request("请给这个账号起个名字（比如「豆包-小号2」）")
    account = new_web_account(provider=provider_slug, name=name)
    store.web_accounts.append(account)
    store.mark_dirty("web_accounts")
    store.save()
    store.flush()
    return _snapshot(account)


@router.patch("/api/web-accounts/{account_id}")
async def patch_web_account(account_id: str, request: Request) -> dict:
    account = find_web_account(account_id)
    if not account:
        not_found("账号不存在")
    body = await json_body(request)
    if body.get("name") is not None and trim(body.get("name")):
        account["name"] = trim(body.get("name"))
    if "enabled" in body:
        account["enabled"] = bool(body.get("enabled"))
    if "remark" in body:
        account["remark"] = trim(body.get("remark"))
    for key in ("loginAt", "loginExpiresAt"):
        if key in body:
            account[key] = as_int(body.get(key), 0)
    if "quotaUsed" in body:
        account["quotaUsed"] = max(0, as_int(body.get("quotaUsed"), 0))
    if "quotaDay" in body:
        account["quotaDay"] = str_or(body.get("quotaDay"))
    if "lastError" in body:
        account["lastError"] = str_or(body.get("lastError"))[:200]
    store.mark_dirty("web_accounts")
    store.save()
    store.flush()
    return _snapshot(account)


@router.delete("/api/web-accounts/{account_id}")
async def delete_web_account(account_id: str) -> dict:
    if not find_web_account(account_id):
        not_found("账号不存在")
    store.web_accounts = [a for a in store.web_accounts if a.get("id") != account_id]
    store.mark_dirty("web_accounts")
    store.save()
    store.flush()
    return ok()


@router.post("/api/web-accounts/pick")
async def pick_account(request: Request) -> dict:
    body = await json_body(request)
    provider_slug = trim(body.get("provider"))
    quota = _quota_of(provider_slug)
    picked = pick_web_account(store.web_accounts, provider_slug, quota)
    if picked:
        return {"account": _snapshot(picked), "reason": ""}
    return {"account": None, "reason": _pick_reason(provider_slug, quota)}


@router.post("/api/web-accounts/{account_id}/commit")
async def commit_usage(account_id: str, request: Request) -> dict:
    account = find_web_account(account_id)
    if not account:
        not_found("账号不存在")
    body = await json_body(request)
    quota = _quota_of(account.get("provider") or "")
    commit_web_usage(account, quota, as_int(body.get("times"), 1) or 1)
    account["lastError"] = ""
    store.mark_dirty("web_accounts")
    store.save()
    store.flush()
    return _snapshot(account)


@router.post("/api/web-accounts/{account_id}/fail")
async def fail_usage(account_id: str, request: Request) -> dict:
    account = find_web_account(account_id)
    if not account:
        not_found("账号不存在")
    body = await json_body(request)
    quota = _quota_of(account.get("provider") or "")
    fail_web_usage(account, body.get("error"))
    store.mark_dirty("web_accounts")
    store.save()
    store.flush()
    return _snapshot(account)


__all__ = ["router"]
