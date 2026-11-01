"""网页平台账号（多账户额度池）—— 复刻 `server/web-accounts.js`（4986 字节）。

## 为什么要有这一层

豆包、千问、元宝这类平台的免费额度是**跟着账号走的**。一个账号一天就那么几次，
多绑几个账号额度就叠加；哪个号今天用完了、或者登录态过期了，就自动跳过换下一个 ——
这才是「额度池」的意义。

和 `store.accounts`（RunningHub 的 API Key 账号）是两套东西，互不干扰：

- `accounts` = API Key 型，调度器直接拿 Key 去调接口
- `web_accounts` = cookie 型，登录态在浏览器的持久化分区里，靠页面自动化用

## 一次生成在账本上怎么记

1. 提交前 `pick_web_account()` 选一个「还有额度 + 登录态有效」的账号
2. 真去生成（Playwright 开窗口跑页面）
3. 成了 `commit_web_usage()` 扣一次；没成不扣（失败不算用户的额度）

额度按**自然日**滚动（0 点重置），不是 24 小时窗口：这些平台的免费额度都是
每天 0 点重算的，用滚动窗口会和平台对不上（平台已经给你补满了，这边还拦着不让用）。
"""

from __future__ import annotations

import random
import time
from datetime import datetime, timedelta

DAY_MS = 24 * 60 * 60 * 1000


def today_key(at: float | None = None) -> str:
    """本地时区的「今天」：'' 表示不限（还没摸清额度就先不设限）"""
    moment = datetime.fromtimestamp((at if at is not None else time.time() * 1000) / 1000)
    return moment.strftime("%Y-%m-%d")


def partition_of(account_id: str) -> str:
    """一个账号一个分区：登录态（cookie）互相看不见"""
    return f"persist:web-{account_id}"


def new_web_account(*, provider: str, name: str) -> dict:
    now = int(time.time() * 1000)
    return {
        "id": f"wa-{_base36(now)}-{_rand4()}",
        "provider": str(provider or "").strip(),
        "name": str(name or "").strip(),
        "enabled": True,
        # 登录态：由桌面壳探测 cookie 后回写
        "loginAt": 0,
        "loginExpiresAt": 0,
        # 额度账本：day 是上次记账的日期，跨天就自动归零
        "quotaDay": "",
        "quotaUsed": 0,
        "stats": {"total": 0, "success": 0, "failed": 0},
        "lastError": "",
        "createdAt": now,
    }


def _base36(value: int) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    if value == 0:
        return "0"
    out = ""
    while value:
        value, rem = divmod(value, 36)
        out = digits[rem] + out
    return out


def _rand4() -> str:
    return "".join(random.choice("0123456789abcdefghijklmnopqrstuvwxyz") for _ in range(4))


def _rollover(account: dict, quota: dict | None) -> int:
    """把账本滚到今天：跨天了就把已用清零。返回已用次数"""
    today = today_key()
    if account.get("quotaDay") != today:
        account["quotaDay"] = today
        account["quotaUsed"] = 0
    return int(account.get("quotaUsed") or 0)


def _daily_limit(quota: dict | None) -> int:
    """这家平台的每日额度上限（0/未配 = 不限）"""
    return max(0, int((quota or {}).get("daily") or 0))


def _cost_of(quota: dict | None) -> int:
    """单次消耗"""
    return max(1, int((quota or {}).get("cost") or 1))


def quota_left(account: dict, quota: dict | None) -> int | None:
    """账号今天还剩几次；None = 不限"""
    limit = _daily_limit(quota)
    if not limit:
        return None
    used = _rollover(account, quota)
    return max(0, limit - used)


def is_usable(account: dict | None, quota: dict | None) -> bool:
    """这个账号现在能不能用：启用 + 今天还有额度（额度不限的永远算有）"""
    if not account or account.get("enabled") is False:
        return False
    left = quota_left(account, quota)
    return left is None or left > 0


def _login_fresh(account: dict) -> bool:
    """库里记的登录态还算数吗：从没登过、或已过期的都不算"""
    if not account.get("loginAt"):
        return False  # 一次都没登过
    expires = int(account.get("loginExpiresAt") or 0)
    if expires and expires <= int(time.time() * 1000):
        return False  # 过期了
    return True


def pick_web_account(accounts: list[dict], provider: str, quota: dict | None) -> dict | None:
    """选一个账号：谁今天剩得多就先用谁，同额度时用得最少的优先。

    为什么不「把一个号用到底」：同一个号短时间内连着生成最容易触发平台风控，
    轮流用反而更稳；而且额度是按天刷新的，早用完和晚用完对总量没区别。

    登录态优先：没登过、或已过期的号排在后面 —— 只有实在没别人了才用它们
    （那时界面上会提示去登录，而不是默默失败）。
    """
    usable = [a for a in (accounts or []) if a.get("provider") == provider and is_usable(a, quota)]
    if not usable:
        return None
    fresh = [a for a in usable if _login_fresh(a)]
    pool = fresh if fresh else usable

    def sort_key(account: dict):
        left = quota_left(account, quota)
        total = int((account.get("stats") or {}).get("total") or 0)
        # 不限额度的（None）排在后面：优先消耗有明确上限、会每天刷新的号
        return (left is None, -(left or 0), total)

    pool.sort(key=sort_key)
    return pool[0]


def commit_web_usage(account: dict, quota: dict | None, times: int = 1) -> int | None:
    """扣一次消耗；返回扣完还剩多少（None = 不限）"""
    _rollover(account, quota)
    cost = _cost_of(quota)
    account["quotaUsed"] = int(account.get("quotaUsed") or 0) + times * cost
    stats = account.setdefault("stats", {"total": 0, "success": 0, "failed": 0})
    stats["total"] = int(stats.get("total") or 0) + times
    stats["success"] = int(stats.get("success") or 0) + times
    return quota_left(account, quota)


def fail_web_usage(account: dict, error) -> None:
    """记一次失败：不扣额度，失败不是用户用掉的"""
    stats = account.setdefault("stats", {"total": 0, "success": 0, "failed": 0})
    stats["total"] = int(stats.get("total") or 0) + 1
    stats["failed"] = int(stats.get("failed") or 0) + 1
    account["lastError"] = str(error or "")[:200]


def next_reset_at() -> int:
    """本地时区的明天 0 点（毫秒）"""
    tomorrow = (datetime.now() + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return int(tomorrow.timestamp() * 1000)


def web_account_snapshot(
    account: dict,
    quota: dict | None,
    *,
    logged: bool = False,
    login_expires_at: int = 0,
) -> dict:
    """给前端的快照：补上算出来的剩余额度、刷新时刻、登录态是否可疑"""
    _rollover(account, quota)
    left = quota_left(account, quota)
    limit = _daily_limit(quota)
    expires = int(login_expires_at or account.get("loginExpiresAt") or 0)
    now = int(time.time() * 1000)
    return {
        **account,
        "quotaLimit": limit or None,
        "quotaLeft": left,
        "quotaLeftUsed": int(account.get("quotaUsed") or 0),
        "quotaResetAt": next_reset_at() if (limit and left is not None and left <= 0) else None,
        # 登录态：现查的结果优先，查不到就用库里记的过期时刻兜底
        "logged": logged,
        "loginExpiresAt": expires,
        "loginStale": bool(expires and expires <= now),
    }

def find_web_account(account_id: str) -> dict | None:
    """按 id 找网页账号（原版到处在用 `db.webAccounts.find(a => a.id === id)`）"""
    from .store import store

    rows = store.web_accounts
    for account in rows:
        if account.get("id") == account_id:
            return account
    return None

