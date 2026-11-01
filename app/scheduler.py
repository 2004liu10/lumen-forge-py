"""RunningHub 账号池调度器 —— 逐函数复刻 `server/scheduler.js`（1427 行）。

## 这一层是整个后端最「难写对」的地方

它管的不是「发一个请求、拿一个结果」，而是三件互相纠缠的事：

1. **账号池**：多个 API Key 轮着用。每个号有并发上限（`concurrency`）、每日免费额度
   （`dailyLimit`，默认 1 次 / 24 小时窗口），还有两种「临时冷却」——
   平台说「并发已满」（`TASK_QUEUE_MAXED`，冷却 60 秒）和
   平台说「余额不足」（`NOT_ENOUGH_BALANCE`，冷却 10 分钟）。
2. **任务生命周期**：queued → preparing → remote_queued → running → (polishing) → success / failed。
   中间任何一步断了，都要能「接管」回来（见 `adopt_orphan_tasks`）。
3. **绝不重复提交**：任务一旦在平台侧建成功（有了 `remoteTaskId`），
   后续无论怎么失败都只能继续等，不能重发 —— 重发等于重复扣费 + 把平台队列顶爆。

## 移植时踩过的坑（都已在代码里处理）

- 原版 `setInterval(tick, 2000)` 在 Python 里换成 asyncio 后台任务；
  `tick` 必须有重入保护，否则上一轮还在派单、下一轮又派一遍，同一个任务被派给两个账号。
- `run_task` 是 fire-and-forget（`tick` 不等它）。Python 里必须 `create_task` 并挂 done-callback
  吞掉异常，否则异常会被 asyncio 的「Task exception was never retrieved」吞掉，
  任务永远挂在 `preparing` 上。
- `await_remote_result` 超过 2 小时**不放弃也不重发**，只是把轮询放慢到 60 秒一次。
  这是原版最重要的一个设计决策，别「优化」掉。
- 网页端兜底（`web-fallback.js`）本期是空壳（见 `app/web_fallback.py`），
  `web_fallback_allowed()` 恒为 False，所以「余额不足就改走网页」这条分支不会触发，
  走的是正常的「换号重试」。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Callable

from .config import (
    KEEP_TASKS,
    LONG_WAIT_POLL_MS,
    MAX_POLL_ERRORS,
    POLL_MS,
    QUEUE_FULL_COOLDOWN_MS,
    QUOTA_WINDOW_MS,
    RESUME_BACKOFF_MS,
    TASK_TIMEOUT_MS,
    TICK_MS,
)
from .ids import next_id, now_ms
from .paths import Paths
from .providers import runninghub as rh
from .store import store

log = logging.getLogger(__name__)

#: 会占用账号并发额度的状态（web_running = 走网页端兜底，同样占着这个号）
ACTIVE_STATES = {"preparing", "remote_queued", "running", "polishing", "web_running"}
PENDING_STATES = {"queued", "preparing", "remote_queued", "running", "polishing", "web_running"}

#: 余额被平台拒绝后的冷处理时长
BALANCE_REJECT_COOLDOWN_MS = 10 * 60 * 1000
#: 余额查询缓存时长
BALANCE_TTL_MS = 5 * 60 * 1000
#: 启动后隔一会儿再查首轮余额（先让首屏渲染出来）
BALANCE_STARTUP_DELAY_MS = 2500
#: 单个结果文件下载超时：大视频 4 分钟还没下完就放弃，只留远端地址
DOWNLOAD_TIMEOUT_MS = 4 * 60 * 1000
#: 每日免费额度默认次数
DEFAULT_DAILY_LIMIT = 1


# ============================================================ 基础查询


def find_account(account_id: str | None) -> dict | None:
    if not account_id:
        return None
    return store.find("accounts", id=account_id)


def find_canvas(canvas_id: str | None) -> dict | None:
    if not canvas_id:
        return None
    return store.find("canvases", id=canvas_id)


def find_task(task_id: str | None) -> dict | None:
    if not task_id:
        return None
    return store.find("tasks", id=task_id)


def running_count(account_id: str) -> int:
    return len(
        [t for t in store.tasks if t.get("accountId") == account_id and t.get("status") in ACTIVE_STATES]
    )


def api_base_url(account: dict | None, canvas: dict | None) -> str:
    """请求走哪个域名：跟账号的站走 —— 国际站（.ai）的 Key 只在 .ai 有效；
    国内站账号沿用画布上填的自定义域名（默认国内站）。
    """
    if (account or {}).get("site") == "ai":
        return rh.base_for_site("ai")
    return (canvas or {}).get("baseUrl") or rh.base_for_site((account or {}).get("site"))


def mask_key(key: str | None) -> str:
    value = str(key or "")
    if len(value) <= 8:
        return "****" if value else ""
    return f"{value[:4]}****{value[-4:]}"


def append_log(task: dict, text: str) -> None:
    task.setdefault("log", [])
    task["log"].append({"t": now_ms(), "text": text})
    if len(task["log"]) > 120:
        del task["log"][: len(task["log"]) - 120]


# ============================================================ 免费额度


def quota_limit(account: dict) -> int:
    """该账号每个窗口期的可用次数；0 = 不限制"""
    raw = account.get("dailyLimit")
    if raw is None or raw == "":
        return DEFAULT_DAILY_LIMIT
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_DAILY_LIMIT
    if value != value or value < 0:  # NaN / 负数
        return DEFAULT_DAILY_LIMIT
    return int(value)


def refresh_quota(account: dict) -> bool:
    """窗口期到期就清零计数；返回是否刚发生刷新"""
    start = account.get("quotaWindowStart")
    if not start:
        return False
    if now_ms() - int(start) < QUOTA_WINDOW_MS:
        return False
    account["quotaUsed"] = 0
    account["quotaWindowStart"] = None
    return True


def quota_left(account: dict) -> int | None:
    """剩余额度；None 表示不限制"""
    limit = quota_limit(account)
    if not limit:
        return None
    return max(0, limit - int(account.get("quotaUsed") or 0))


def quota_blocked(account: dict) -> bool:
    left = quota_left(account)
    return left is not None and left <= 0


def release_quota_if_funded(account: dict) -> bool:
    """余额里确实还有钱（RH 币 > 0）时，撤掉本地的「免费额度」冷却。

    本地这个计数器只是一层节流；账号能不能跑的真实依据是平台的 RH 币余额。
    以前只认本地计数，于是会出现「平台账户里明明有 100 RH 币，界面却挂着额度用尽、
    调度也不给派单」的自相矛盾状态 —— 必须在每次查到余额后按真实余额解锁。
    """
    balance = account.get("balance") or {}
    if balance.get("error"):
        return False
    coins = balance.get("remainCoins")
    try:
        coins_num = float(coins)
    except (TypeError, ValueError):
        return False
    if coins_num <= 0:
        return False
    if not quota_blocked(account):
        return False
    account["quotaUsed"] = 0
    account["quotaWindowStart"] = None
    return True


def consume_quota(account: dict) -> int | None:
    """生成成功后消费一次额度；进入冷却则返回刷新时间，否则返回 None"""
    limit = quota_limit(account)
    if not limit:
        return None
    account["quotaWindowStart"] = account.get("quotaWindowStart") or now_ms()
    account["quotaUsed"] = int(account.get("quotaUsed") or 0) + 1
    return int(account["quotaWindowStart"]) + QUOTA_WINDOW_MS if (quota_left(account) or 0) <= 0 else None


def account_snapshot(account: dict) -> dict:
    refresh_quota(account)
    used = running_count(account["id"])
    left = quota_left(account)
    window_start = account.get("quotaWindowStart")
    return {
        **account,
        "apiKey": mask_key(account.get("apiKey")),
        "hasKey": bool(account.get("apiKey")),
        "running": used,
        "capacity": max(1, int(account.get("concurrency") or 1)),
        "quotaLimit": quota_limit(account) or None,
        "quotaLeft": left,
        # 额度用尽时给出刷新时间，前端据此倒计时
        "quotaResetAt": (int(window_start) + QUOTA_WINDOW_MS) if (left is not None and left <= 0 and window_start) else None,
    }


# ============================================================ RH 币余额

#: 同一账号的并发查询去重，避免定时轮询和手动刷新撞在一起
_balance_inflight: dict[str, asyncio.Future] = {}


async def refresh_account_balance(account: dict, *, force: bool = False) -> dict | None:
    """拉一次账号余额写回账号对象。查询失败也记在 balance.error 上，
    界面上就能分清「查不到」和「真的没额度」。
    """
    if not account.get("apiKey"):
        return account.get("balance")
    balance = account.get("balance") or {}
    if not force and now_ms() - int(balance.get("updatedAt") or 0) < BALANCE_TTL_MS:
        return balance

    pending = _balance_inflight.get(account["id"])
    if pending is not None:
        return await asyncio.shield(pending)

    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    _balance_inflight[account["id"]] = fut
    try:
        try:
            data = await rh.fetch_account_status(
                base_url=rh.base_for_site(account.get("site")), api_key=account["apiKey"]
            )
        except Exception as err:  # noqa: BLE001 - 查询失败不该影响调度
            account["balance"] = {**(account.get("balance") or {}), "updatedAt": now_ms(), "error": str(err)}
            store.mark_dirty("accounts")
            result = account["balance"]
        else:
            account["balance"] = {**data, "updatedAt": now_ms(), "error": ""}
            # 查到余额确实还有：立刻把本地的「额度用尽」撤掉，界面提示随之消失、调度也重新可用
            release_quota_if_funded(account)
            # 之前若刚被平台拒过「余额不足」：只有余额真的变多了（充了值 / 领了币）才解除冷处理，
            # 否则失败后紧跟的这次回查会把冷却立刻清掉，重试又去撞同一个被拒的号
            if int(account.get("balanceRejectedUntil") or 0) > now_ms():
                coins = float(data.get("remainCoins") or 0)
                money = float(data.get("remainMoney") or 0)
                if account.get("balanceRejectedCoins") is None and account.get("balanceRejectedMoney") is None:
                    # 被拒那一刻还没查过余额：这次回查的数据定为基线，不解除
                    account["balanceRejectedCoins"] = coins
                    account["balanceRejectedMoney"] = money
                elif coins > float(account.get("balanceRejectedCoins") or 0) or money > float(
                    account.get("balanceRejectedMoney") or 0
                ):
                    account["balanceRejectedUntil"] = 0
                    account["balanceRejectedCoins"] = None
                    account["balanceRejectedMoney"] = None
            store.mark_dirty("accounts")
            result = account["balance"]
        if not fut.done():
            fut.set_result(result)
        return result
    finally:
        _balance_inflight.pop(account["id"], None)


async def _balance_watch_loop() -> None:
    """后台定时刷新已启用且有 Key 的账号。

    这里必须 force：定时轮询本身就是 5 分钟一轮，跟 TTL 一样长，
    而启动那次又比整点晚 2.5 秒 —— 不 force 的话每轮都「差一点点没到期」被跳过，
    实际变成 10 分钟才查一次，界面上的余额和「额度用尽」会长时间停在错误状态。
    """
    await asyncio.sleep(BALANCE_STARTUP_DELAY_MS / 1000)
    while True:
        for account in list(store.accounts):
            if not account.get("enabled") or not account.get("apiKey"):
                continue
            try:
                await refresh_account_balance(account, force=True)
            except Exception:  # noqa: BLE001 - 单账号失败不影响其他账号
                pass
        await asyncio.sleep(BALANCE_TTL_MS / 1000)


# ============================================================ 错误分类


def is_balance_short(message: str | None) -> bool:
    return bool(re.search(r"NOT_ENOUGH_BALANCE|余额不足|REMAIN_BALANCE", str(message or "")))


def balance_short_text(account: dict) -> str:
    """余额被平台拒绝时，光说「余额不足」没用 —— RH 币和 API Key 类型是两回事，这里点明排查方向。"""
    return (
        f"账号「{account.get('name')}」被平台拒绝（NOT_ENOUGH_BALANCE）。"
        "这个判断是 RunningHub 平台做的：这次任务要扣的币超过了账户余额（程序本地不拦提交，再提交也是平台拒）。"
        "只有「消费级-会员」类型的 Key 才扣每日赠送的 RH 币，「企业级-共享」类型的 Key 按秒扣「钱包余额」；"
        "也可能是这个应用单次消耗就超过了账户里的币。请确认 Key 类型，或换余额够的账号 / 充值钱包"
    )


async def mark_balance_rejected(account: dict | None) -> None:
    """平台按「余额不足」拒了这次提交：给这个账号挂冷却、记下被拒那一刻的余额基线。

    抽成函数是因为「改用网页端跑」这条路也要挂冷却 —— API 那本账确实扣不动，
    别让下一次提交又来撞同一个号；而网页端跑完可能让余额变多，回查会按基线自动解除。
    """
    if not account:
        return
    account["balanceRejectedUntil"] = now_ms() + BALANCE_REJECT_COOLDOWN_MS
    balance = account.get("balance") or {}
    account["balanceRejectedCoins"] = balance.get("remainCoins")
    account["balanceRejectedMoney"] = balance.get("remainMoney")
    store.mark_dirty("accounts")
    try:
        await refresh_account_balance(account, force=True)
    except Exception:  # noqa: BLE001
        pass


def is_queue_maxed(message: str | None) -> bool:
    """平台按「账号」限制同时在跑的任务数，超了会回 TASK_QUEUE_MAXED。
    这不是任务本身的错（往往是这个账号上一个还没跑完的任务占着位），所以既不算失败次数，
    也不能判定任务失败，等这个账号空闲下来再提交就好。
    """
    return bool(re.search(r"TASK_QUEUE_MAXED", str(message or "")))


def queue_cooling(account: dict) -> bool:
    return int(account.get("queueFullUntil") or 0) > now_ms()


def balance_cooling(account: dict) -> bool:
    """刚被平台拒绝过「余额不足」的账号，短时间内别再撞它。

    注意：余额够不够是**平台在创建任务那一刻**判断的（任务消耗 > 账户余额就拒），
    程序本地从不拦截提交 —— 这里的冷却只是别把重试次数浪费在明知道会被拒的号上，
    让重试去试别的账号。账号一刷新余额（充值 / 领币后点「查余额」）立即解除。
    """
    return int(account.get("balanceRejectedUntil") or 0) > now_ms()


# ============================================================ 派单


def usable_accounts() -> list[dict]:
    """可用账号 = 已启用 + 有 Key + 免费额度未用尽"""
    out = []
    for account in store.accounts:
        refresh_quota(account)
        if account.get("enabled") and account.get("apiKey") and not quota_blocked(account):
            out.append(account)
    return out


def pick_account(pinned_id: str | None = None) -> dict | None:
    pool = [a for a in usable_accounts() if not queue_cooling(a) and not balance_cooling(a)]
    if pinned_id:
        # 画布绑定了账号：尊重绑定，但如果这个号刚被平台拒过余额不足，
        # 再撞一次也是白烧一次尝试 —— 松开绑定让任务去试其它账号
        pinned = next((a for a in pool if a["id"] == pinned_id), None)
        if not pinned:
            return None
        return pinned if running_count(pinned["id"]) < int(pinned.get("concurrency") or 1) else None

    free = [a for a in pool if running_count(a["id"]) < int(a.get("concurrency") or 1)]
    if not free:
        return None
    free.sort(
        key=lambda a: (
            running_count(a["id"]),
            int((a.get("stats") or {}).get("total") or 0),
            int(a.get("createdAt") or 0),
        )
    )
    return free[0]


def blocked_reason(pinned_id: str | None = None) -> str:
    """任务迟迟派不出去时，给出人能看懂的原因"""
    ready = [a for a in store.accounts if a.get("enabled") and a.get("apiKey")]
    for account in ready:
        refresh_quota(account)
    if not ready:
        return "没有可用账号，请先在「账号管理」添加 API Key"

    blocked = [a for a in ready if quota_blocked(a)]
    if len(blocked) == len(ready):
        reset_at = min(int(a.get("quotaWindowStart") or now_ms()) + QUOTA_WINDOW_MS for a in blocked)
        return f"所有账号免费额度已用尽，{_fmt_time(reset_at)} 后自动刷新"
    if pinned_id and any(a["id"] == pinned_id and quota_blocked(a) for a in ready):
        pinned = next((a for a in ready if a["id"] == pinned_id), None)
        return f"指定账号「{(pinned or {}).get('name') or pinned_id}」免费额度已用尽，等待刷新或改绑其它账号"
    # 都刚被平台说过「并发已满」：不是失败，等几十秒会自动重试
    if all(queue_cooling(a) for a in ready):
        return "账号并发已满，稍后自动重试"
    # 都刚被平台拒过「余额不足」：等冷却结束再试，或充值 / 领币后点「查余额」立即解除
    cooling = [a for a in ready if balance_cooling(a)]
    if len(cooling) == len(ready):
        reset_at = min(int(a["balanceRejectedUntil"]) for a in cooling)
        return (
            "各账号刚被平台提示余额不足（这次任务的消耗超过了账户里的 RH 币/钱包）。程序不会拦提交，"
            f"等 {max(1, round((reset_at - now_ms()) / 60000))} 分钟后会自动再试；"
            "充值或领币后点「查余额」可立即解除"
        )
    if pinned_id and any(a["id"] == pinned_id and balance_cooling(a) for a in ready):
        return "指定账号刚被平台提示余额不足，稍后自动换号重试（或解除画布的账号绑定）"
    if pinned_id and any(a["id"] == pinned_id and queue_cooling(a) for a in ready):
        pinned = next((a for a in ready if a["id"] == pinned_id), None)
        return f"指定账号「{(pinned or {}).get('name') or pinned_id}」并发已满，稍后自动重试"
    return "账号都在忙，排队等待中"


def _fmt_time(ms: int) -> str:
    return time.strftime("%Y/%m/%d %H:%M:%S", time.localtime(ms / 1000))


# ============================================================ 任务创建 / 取消 / 重试


def create_task(canvas: dict, *, pinned_account_id: str | None = None) -> dict:
    if not canvas.get("appId"):
        raise ValueError("请先填写 webappId / workflowId 并点击「读取参数」")
    if not canvas.get("fields"):
        raise ValueError("没有可提交的参数，请先点击「读取参数」")

    task = {
        "id": next_id("tk"),
        "canvasId": canvas["id"],
        "canvasName": canvas.get("name") or "",
        "mode": canvas.get("mode"),
        "appId": canvas.get("appId"),
        "status": "queued",
        "message": "排队中",
        "attempts": 0,
        "maxAttempts": 1 if canvas.get("autoRetry") is False else max(1, int(canvas.get("maxAttempts") or 2)),
        "accountId": None,
        "accountName": None,
        "pinnedAccountId": pinned_account_id or None,
        "remoteTaskId": None,
        "outputs": [],
        "error": None,
        "log": [],
        "createdAt": now_ms(),
        "startedAt": None,
        "finishedAt": None,
        "cancelRequested": False,
    }
    append_log(task, "任务已加入队列")
    store.tasks.insert(0, task)
    store.mark_dirty("tasks")
    store.save()
    return task


def cancel_task_request(task: dict) -> dict:
    if task.get("status") == "queued":
        task["status"] = "canceled"
        task["message"] = "已取消"
        task["finishedAt"] = now_ms()
        append_log(task, "已取消（尚未派发）")
        store.mark_dirty("tasks")
        store.save()
        return task
    if task.get("status") in PENDING_STATES:
        task["cancelRequested"] = True
        task["message"] = "取消中…"
        append_log(task, "已请求取消")
        store.mark_dirty("tasks")
        store.save()
    return task


def retry_task(task: dict) -> dict:
    if task.get("status") in PENDING_STATES:
        raise ValueError("任务仍在进行中")
    task.update(
        {
            "status": "queued",
            "message": "排队中",
            "error": None,
            "remoteTaskId": None,
            "accountId": None,
            "accountName": None,
            "cancelRequested": False,
            "outputs": [],
            "attempts": 0,
            "startedAt": None,
            "finishedAt": None,
            # 手动重生成是一次全新的尝试，画布绑定恢复生效
            "ignoreCanvasPin": False,
        }
    )
    append_log(task, "重新排队")
    store.mark_dirty("tasks")
    store.save()
    return task


# ============================================================ 调度循环


class _State:
    ticking = False
    #: 本进程正在盯着（上传/等待结果）的任务，避免重启接管时重复轮询
    active_task_ids: set[str] = set()
    task: asyncio.Task | None = None
    balance_task: asyncio.Task | None = None


def _spawn(coro: Any, *, label: str) -> asyncio.Task:
    """起一个后台协程并吞掉异常。

    `tick()` / `run_task()` 是 fire-and-forget 的，异常不吞掉的话
    asyncio 只会打一行 "Task exception was never retrieved"，任务就永久挂在中间态。
    """
    task = asyncio.get_running_loop().create_task(coro)

    def _done(t: asyncio.Task) -> None:
        if t.cancelled():
            return
        err = t.exception()
        if err is not None:
            log.warning("[scheduler] %s 未捕获异常：%s", label, err)

    task.add_done_callback(_done)
    return task


def adopt_orphan_tasks() -> None:
    """接管「已经提交到平台、但本地没有进程在等结果」的任务。

    视频动辄跑十几分钟，中途关一次程序不该把结果白扔。
    """
    for task in list(store.tasks):
        # queued 的由下面的正常派单流程处理，别在这里重复排队
        if task.get("status") not in PENDING_STATES or task.get("status") == "queued":
            continue
        if task["id"] in _State.active_task_ids:
            continue
        # 上次连接不稳时约好的「过一会儿再来」：没到点先别急着轮询
        if int(task.get("nextResumeAt") or 0) > now_ms():
            continue

        # 还没提交出去的（进程死在准备阶段）：重新排队，走正常流程再来一遍
        if not task.get("remoteTaskId") and task.get("status") != "polishing":
            task["status"] = "queued"
            task["message"] = "重启后重新排队"
            task["accountId"] = None
            task["accountName"] = None
            append_log(task, "程序重启时这个任务还没提交到平台，已重新排队")
            store.mark_dirty("tasks")
            continue

        canvas = find_canvas(task.get("canvasId"))
        account = find_account(task.get("accountId"))
        if not canvas or not account:
            # 画布或账号没了，接管不了，直接判失败，免得永远挂在「进行中」
            task["status"] = "failed"
            task["message"] = "程序重启后无法接管"
            task["error"] = "原账号已不存在" if canvas else "原画布已删除"
            task["finishedAt"] = now_ms()
            append_log(task, f"失败：{task['error']}")
            store.mark_dirty("tasks")
            continue
        _spawn(resume_task(task, canvas, account), label=f"resume {task['id']}")


async def resume_task(task: dict, canvas: dict, account: dict) -> None:
    """接手一个已经提交到平台的任务，继续等它的结果"""
    _State.active_task_ids.add(task["id"])
    task["nextResumeAt"] = 0  # 开始接管了，清掉等待时刻
    failed: Exception | None = None
    try:
        if task.get("status") == "polishing":
            # 去水印跑到一半被关了：原视频已经下载好了，收尾成成功，别让结果卡住
            append_log(task, "程序重启时正在去水印，已保留原视频（未去水印）")
            task["status"] = "success"
            task["message"] = "已完成"
            task["finishedAt"] = now_ms()
            return
        append_log(task, f"程序重启，继续等待平台结果（远端任务 {task.get('remoteTaskId')}）")
        task["message"] = "重启后继续等待结果"
        store.mark_dirty("tasks")
        store.save()
        await await_remote_result(task, canvas, account)
    except Exception as err:  # noqa: BLE001
        failed = err
    finally:
        _State.active_task_ids.discard(task["id"])
        if failed is not None:
            await handle_failure(task, canvas, account, failed)
        store.mark_dirty("tasks")
        store.save()
        store.flush()


async def tick() -> None:
    if _State.ticking:
        return
    _State.ticking = True
    try:
        adopt_orphan_tasks()
        queued = sorted(
            [t for t in store.tasks if t.get("status") == "queued"],
            key=lambda t: int(t.get("createdAt") or 0),
        )

        for task in queued:
            if task.get("cancelRequested"):
                cancel_task_request(task)
                continue
            canvas = find_canvas(task.get("canvasId"))
            if not canvas:
                task["status"] = "failed"
                task["error"] = "画布已被删除"
                task["message"] = task["error"]
                task["finishedAt"] = now_ms()
                store.mark_dirty("tasks")
                continue
            # 余额被拒过的重试会带 ignoreCanvasPin：画布绑定的账号刚被平台拒过，
            # 再派给它也是白烧一次尝试，这次换其它账号
            pinned_id = None if task.get("ignoreCanvasPin") else (canvas.get("accountId") or task.get("pinnedAccountId") or None)
            account = pick_account(pinned_id)
            if not account:
                # 没有空闲账号：把排队原因写进任务，方便界面上看出是在等额度还是等并发
                reason = blocked_reason(pinned_id)
                if task.get("message") != reason:
                    task["message"] = reason
                    store.mark_dirty("tasks")
                continue

            task["accountId"] = account["id"]
            task["accountName"] = account.get("name")
            task["attempts"] = int(task.get("attempts") or 0) + 1
            task["status"] = "preparing"
            task["message"] = "准备素材与参数…"
            task["startedAt"] = task.get("startedAt") or now_ms()
            append_log(task, f"账号「{account.get('name')}」已接单（第 {task['attempts']} 次尝试）")
            store.mark_dirty("tasks")
            store.save()
            _spawn(run_task(task, canvas, account), label=f"run {task['id']}")
    finally:
        _State.ticking = False


async def _tick_loop() -> None:
    while True:
        try:
            await tick()
        except Exception as err:  # noqa: BLE001 - 一轮失败不该让整个调度停摆
            log.warning("[scheduler] tick 异常：%s", err)
        await asyncio.sleep(TICK_MS / 1000)


def start_scheduler() -> None:
    """启动调度循环与余额轮询。幂等 —— 重复调用只会重新起一遍。"""
    if _State.task is None or _State.task.done():
        _State.task = _spawn(_tick_loop(), label="tick-loop")
    if _State.balance_task is None or _State.balance_task.done():
        _State.balance_task = _spawn(_balance_watch_loop(), label="balance-loop")


async def stop_scheduler() -> None:
    for attr in ("task", "balance_task"):
        task = getattr(_State, attr)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        setattr(_State, attr, None)
    _State.active_task_ids.clear()


# ============================================================ 参数装配


def field_key(field: dict) -> str:
    return f"{field.get('nodeId')}::{field.get('fieldName')}"


def rh_field_is_file(field: dict) -> bool:
    return str(field.get("fieldType") or "").upper() in ("IMAGE", "VIDEO", "AUDIO")


ASSET_EXT_RE = {
    "IMAGE": re.compile(r"\.(png|jpe?g|webp|bmp|gif|tiff?)$", re.I),
    "VIDEO": re.compile(r"\.(mp4|mov|webm|mkv|avi|m4v)$", re.I),
    "AUDIO": re.compile(r"\.(mp3|wav|m4a|aac|flac|ogg)$", re.I),
}


def asset_kind_mismatch(field: dict, asset: dict) -> str:
    """素材类型和字段类型对不上时给人话提示。

    两个文件字段选反（视频传给了「加载参考图片」）会让平台报
    `ref_pose_keypoint must contain exactly one frame`，很难看出是选错文件，
    所以提交前先拦下来，省得白占一次排队。
    """
    name = str(asset.get("originalName") or asset.get("localPath") or "")
    type_ = str(field.get("fieldType") or "").upper()
    kind = next((k for k, rx in ASSET_EXT_RE.items() if rx.search(name)), None)
    if not kind or kind == type_:
        return ""
    label = field.get("description") or field.get("fieldName")
    return (
        f"文件可能选反了：字段「{label}」是 {type_} 类型，却绑了 {kind} 文件「{asset.get('originalName')}」。"
        "请到画布上检查各个文件字段有没有对应错"
    )


async def build_node_info_list(canvas: dict, account: dict, task: dict) -> list[dict]:
    base_url = api_base_url(account, canvas)
    out: list[dict] = []

    for field in canvas.get("fields") or []:
        key = field_key(field)
        if rh_field_is_file(field):
            asset = (canvas.get("assets") or {}).get(key)
            if not asset:
                continue
            mismatch = asset_kind_mismatch(field, asset)
            if mismatch:
                # 参数没配对，重试多少次都一样，直接失败让用户去改画布
                err = ValueError(mismatch)
                setattr(err, "fatal", True)
                raise err
            append_log(task, f"上传素材「{asset.get('originalName')}」→ {field.get('description') or field.get('fieldName')}")
            local_path = Path(str(asset.get("localPath") or ""))
            if not local_path.exists():
                err = ValueError(f"素材文件已不存在：{asset.get('originalName')}")
                setattr(err, "fatal", True)
                raise err
            buffer = local_path.read_bytes()
            remote_name = await rh.upload_file(
                base_url=base_url,
                api_key=account["apiKey"],
                buffer=buffer,
                filename=str(asset.get("originalName") or local_path.name),
                mime_type=asset.get("mimeType"),
                file_type="input",
            )
            item = {
                "nodeId": str(field.get("nodeId")),
                "fieldName": field.get("fieldName"),
                "fieldValue": remote_name,
            }
            if field.get("description"):
                item["description"] = field["description"]
            out.append(item)
        else:
            value = (canvas.get("values") or {}).get(key)
            if value is None or value == "":
                continue
            item = {
                "nodeId": str(field.get("nodeId")),
                "fieldName": field.get("fieldName"),
                "fieldValue": _coerce_number(field, value),
            }
            if field.get("description"):
                item["description"] = field["description"]
            out.append(item)

    # 额外参数（逃生舱）：直接把 JSON 数组并入 nodeInfoList，用于覆盖未在界面上暴露的节点
    extra = str(canvas.get("extraParams") or "").strip()
    if extra:
        try:
            parsed = json.loads(extra)
        except json.JSONDecodeError:
            append_log(task, "额外参数 JSON 解析失败，已忽略")
        else:
            if isinstance(parsed, list):
                for item in parsed:
                    if isinstance(item, dict) and "nodeId" in item and "fieldName" in item:
                        out.append(
                            {
                                "nodeId": str(item["nodeId"]),
                                "fieldName": str(item["fieldName"]),
                                "fieldValue": item.get("fieldValue"),
                            }
                        )

    if not out:
        raise ValueError("没有可提交的参数：请先「读取参数」，再上传素材或填写数值")
    return out


def _coerce_number(field: dict, value: Any) -> Any:
    if str(field.get("fieldType") or "").upper() == "NUMBER":
        try:
            return float(value) if "." in str(value) else int(value)
        except (TypeError, ValueError):
            return value
    return value


# ============================================================ 水印参数


WATERMARK_RE = re.compile(r"watermark|水印|aigc", re.I)
#: LIST 选项里表示「关掉」的写法，中英文都认
OFF_LABEL_RE = re.compile(r"^(false|no|off|0|否|关闭|不|禁用|不要|无水印|不加水印)$", re.I)


def watermark_off_value(field: dict) -> Any:
    """判断字段是不是水印开关；是的话给出「关闭」应填的值，判断不了返回 None。

    拿不准就返回 None，宁可不改也不要乱改用户的参数。
    """
    hay = f"{field.get('fieldName') or ''} {field.get('nodeName') or ''} {field.get('description') or ''}"
    if not WATERMARK_RE.search(hay):
        return None

    options = field.get("options") or []
    if options:
        off = next((o for o in options if OFF_LABEL_RE.match(str(o.get("label") or "").strip())), None)
        return str(off.get("index")) if off else None
    type_ = str(field.get("fieldType") or "").upper()
    if type_ == "NUMBER":
        return 0
    if type_ == "LIST":
        # 工作流模式下的布尔节点没有选项列表，默认值长得像布尔就按 false 处理
        default = field.get("defaultValue")
        return "false" if re.match(r"^(true|false)$", str("" if default is None else default).strip(), re.I) else None
    if type_ == "STRING":
        return "false"
    return None


def apply_watermark_off(canvas: dict) -> list[dict]:
    """按画布设置把水印参数改成关闭值，直接写进 canvas.values（界面上看得见、也能改）。
    返回被改动的字段列表，调用方据此写日志或回显。
    """
    changed: list[dict] = []
    if canvas.get("removeWatermark") is False:
        return changed
    canvas["values"] = canvas.get("values") or {}
    for field in canvas.get("fields") or []:
        off = watermark_off_value(field)
        if off is None:
            continue
        key = field_key(field)
        current = canvas["values"].get(key)
        if str("" if current is None else current) == str(off):
            continue
        canvas["values"][key] = off
        changed.append({"field": field, "value": off})
    return changed


# ============================================================ 结果下载


def guess_extension(file_type: str | None, file_url: str | None) -> str:
    from_type = re.sub(r"[^a-z0-9]", "", str(file_type or "").lower())
    if from_type:
        return f".{from_type}"
    try:
        ext = Path(str(file_url or "")).suffix.lstrip(".").lower()
        if ext:
            return f".{ext}"
    except (ValueError, OSError):
        pass
    return ".bin"


def is_video_output(item: dict) -> bool:
    """结果里哪些是视频：看扩展名，其次看平台给的 fileType"""
    url = str(item.get("localUrl") or item.get("fileUrl") or "")
    if re.search(r"\.(mp4|mov|webm|mkv|avi|m4v)(\?|$)", url, re.I):
        return True
    return bool(re.search(r"video", str(item.get("fileType") or ""), re.I))


async def download_outputs(task: dict, outputs: list[dict], prefix: str = "", subdir: str = "") -> list[dict]:
    import httpx

    saved: list[dict] = []
    # subdir 给了就落到 OUTPUT_DIR 下的子文件夹里（一键取回按当天日期归档用）
    paths: Paths = get_paths()
    directory = paths.outputs / subdir if subdir else paths.outputs
    directory.mkdir(parents=True, exist_ok=True)

    async with httpx.AsyncClient(timeout=DOWNLOAD_TIMEOUT_MS / 1000, follow_redirects=True) as client:
        for index, item in enumerate(outputs):
            ext = guess_extension(item.get("fileType"), item.get("fileUrl"))
            name = f"{task['id']}{prefix}_{index + 1}{ext}"
            dest = directory / name
            try:
                # 必须带超时：这是全项目唯一一个没套超时的外网请求。
                # 远端慢速挂起时这条任务会一直占着账号并发额度。
                res = await client.get(str(item.get("fileUrl") or ""))
                if res.status_code >= 400:
                    raise RuntimeError(f"HTTP {res.status_code}")
                dest.write_bytes(res.content)
                saved.append(
                    {
                        **item,
                        "localUrl": f"/files/outputs/{subdir + '/' if subdir else ''}{name}",
                        "localPath": str(dest),
                        "size": len(res.content),
                    }
                )
            except Exception as err:  # noqa: BLE001 - 单个文件失败不该让整个任务失败
                append_log(task, f"结果下载失败，仅保留远端地址：{err}")
                saved.append({**item, "localUrl": None, "localPath": None, "downloadError": str(err)})
    return saved


#: 由 main.py 注入的 Paths 实例（存储位置可被用户改，所以不能是模块级常量）
_paths: Paths | None = None


def set_paths(paths: Paths) -> None:
    global _paths
    _paths = paths


def get_paths() -> Paths:
    if _paths is None:
        raise RuntimeError("调度器还没拿到存储位置（main.py 启动时先调 scheduler.set_paths）")
    return _paths


# ============================================================ 去水印（空壳）


def pick_video_field(fields: list[dict]) -> dict | None:
    files = [f for f in (fields or []) if rh_field_is_file(f)]
    return (
        next((f for f in files if str(f.get("fieldType") or "").upper() == "VIDEO"), None)
        or next(
            (f for f in files if re.search(r"video|视频", f"{f.get('fieldName')} {f.get('description')} {f.get('nodeName')}", re.I)),
            None,
        )
        or (files[0] if files else None)
    )


async def polish_video_outputs(task: dict, canvas: dict, account: dict, *, prefer_web: bool = False) -> None:
    """把生成出来的视频送进「去水印应用」再跑一遍，用干净版覆盖结果。

    **本期未实现**：这需要 `rh.inspect_app` + 上传 + 建任务 + 轮询 + 下载的完整链路，
    而它依赖的 `web-fallback.js`（网页端去水印）在 Python 版是空壳。
    调用方已经用 try/except 包住，失败只记日志、保留原视频 —— 与 Node 版行为一致。
    """
    append_log(task, "去水印功能本期未移植，已保留原视频")


# ============================================================ 网页端兜底（空壳）


def web_fallback_allowed(canvas: dict) -> bool:
    """这个画布能不能改用网页端跑。

    网页版（没有内嵌浏览器）里这条能力整条不可用，照常走换号重试。
    本期 `web_fallback` 是空壳，所以恒为 False。
    """
    return False


async def run_via_web(task: dict, canvas: dict, account: dict) -> None:
    raise RuntimeError("网页端兜底本期未移植")


# ============================================================ 单任务执行


async def run_task(task: dict, canvas: dict, account: dict) -> None:
    _State.active_task_ids.add(task["id"])
    failed: Exception | None = None
    try:
        node_info_list = await build_node_info_list(canvas, account, task)

        base_url = api_base_url(account, canvas)
        created = (
            await rh.create_workflow_task(
                base_url=base_url,
                api_key=account["apiKey"],
                workflow_id=str(canvas.get("appId")),
                instance_type=canvas.get("instanceType"),
                node_info_list=node_info_list,
            )
            if canvas.get("mode") == "workflow"
            else await rh.create_ai_app_task(
                base_url=base_url,
                api_key=account["apiKey"],
                app_id=str(canvas.get("appId")),
                instance_type=canvas.get("instanceType"),
                node_info_list=node_info_list,
            )
        )

        task["remoteTaskId"] = str((created.get("data") or {}).get("taskId"))
        tips = rh.parse_prompt_tips((created.get("data") or {}).get("promptTips"))
        node_errors = (tips or {}).get("node_errors")
        if node_errors and isinstance(node_errors, dict) and node_errors:
            append_log(task, f"节点错误提示：{json.dumps(node_errors, ensure_ascii=False)[:400]}")
        task["status"] = "remote_queued"
        task["message"] = "已提交，排队中"
        append_log(task, f"远端任务已创建：{task['remoteTaskId']}")
        # 额度从「提交成功」这一刻起算（以前要等生成成功才扣、才开始倒计时）：
        # 任务一提交就占住了这个账号，24 小时窗口该跟着开始走，而不是等生成完再开始
        quota_reset_at = consume_quota(account)
        if quota_reset_at:
            append_log(task, f"账号「{account.get('name')}」免费额度已用尽，{_fmt_time(quota_reset_at)} 后自动刷新")
        store.mark_dirty("tasks")
        store.mark_dirty("accounts")
        store.save()

        await await_remote_result(task, canvas, account)
        return
    except Exception as err:  # noqa: BLE001
        failed = err
        # 平台说这个号「余额不足」：先在本地记一笔冷却（API 那本账确实扣不动），
        # 再改用网页端跑 —— 同一个号在网页里往往还有币，能直接出片。
        if is_balance_short(str(err)) and not task.get("remoteTaskId") and web_fallback_allowed(canvas):
            await mark_balance_rejected(account)
            # 网页端点一次「立即运行」就是真扣币。同一任务连跑两次都没成，多半是页面自动化对不上，
            # 再换号重试只会继续烧币 —— 宁可停下来让人去网页上手动跑。
            if int(task.get("webRuns") or 0) >= 2:
                append_log(task, f"网页端已经跑过 {task.get('webRuns')} 次都没拿到成片，不再自动重试（避免继续扣币），请到网页上手动运行")
                failed = err
            else:
                try:
                    await run_via_web(task, canvas, account)
                    failed = None
                    return
                except Exception as web_err:  # noqa: BLE001
                    append_log(task, f"网页端兜底失败：{web_err}")
                    failed = web_err
    finally:
        _State.active_task_ids.discard(task["id"])
        outcome = await handle_failure(task, canvas, account, failed) if failed is not None else ""
        # 「账号并发已满」是账号忙，不是这个任务的成绩，别记进统计里干扰派单排序
        if outcome != "queue_maxed":
            account.setdefault("stats", {})
            account["stats"]["total"] = int(account["stats"].get("total") or 0) + 1
        store.mark_dirty("tasks")
        store.mark_dirty("accounts")
        store.save()
        store.flush()


async def await_remote_result(task: dict, canvas: dict, account: dict) -> None:
    """轮询远端任务直到出结果。

    抽成独立函数是为了让「程序重启后接管」能复用同一套逻辑，而不是重发一次任务。
    """
    base_url = api_base_url(account, canvas)
    deadline = now_ms() + TASK_TIMEOUT_MS
    long_wait = False
    poll_errors = 0

    while True:
        await asyncio.sleep((LONG_WAIT_POLL_MS if long_wait else POLL_MS) / 1000)

        if task.get("cancelRequested"):
            try:
                await rh.cancel_task(base_url=base_url, api_key=account["apiKey"], task_id=str(task.get("remoteTaskId")))
            except Exception as err:  # noqa: BLE001
                append_log(task, f"远端取消失败：{err}")
            task["status"] = "canceled"
            task["message"] = "已取消"
            task["finishedAt"] = now_ms()
            append_log(task, "已取消")
            store.mark_dirty("tasks")
            store.save()
            return

        # 超过「快轮询」上限**不放弃也不重发**：任务已经在平台侧提交、钱也扣了，
        # 丢掉它才是真的白花钱。改成慢慢等（每分钟查一次），直到平台给出结果或明确失败。
        if not long_wait and now_ms() > deadline:
            long_wait = True
            append_log(task, "等待已超过 2 小时，改为每分钟检查一次（不会重发任务）")
            store.mark_dirty("tasks")
            store.save()

        try:
            result = await rh.query_outputs(
                base_url=base_url, api_key=account["apiKey"], task_id=str(task.get("remoteTaskId"))
            )
            poll_errors = 0
        except Exception as err:  # noqa: BLE001
            # 网络抖一下不该把已经扣费的任务判死：连着失败够多次才交给上层处理（上层也不会重发）
            poll_errors += 1
            append_log(task, f"查询结果失败（第 {poll_errors}/{MAX_POLL_ERRORS} 次）：{err}")
            if poll_errors >= MAX_POLL_ERRORS:
                raise RuntimeError(f"连续 {MAX_POLL_ERRORS} 次查询结果失败：{err}") from err
            store.mark_dirty("tasks")
            store.save()
            continue

        state = result.get("state")
        if state == "queued":
            task["status"] = "remote_queued"
            task["message"] = "排队中"
        elif state == "running":
            task["status"] = "running"
            task["message"] = "生成中"
        elif state == "success":
            task["outputs"] = await download_outputs(task, result.get("outputs") or [])
            account.setdefault("stats", {})
            account["stats"]["success"] = int(account["stats"].get("success") or 0) + 1
            # 额度已经在提交那一刻扣过了（见 run_task），这里只回查一次余额
            # 成功一次就会扣 RH 币，立刻回查一次，界面上能马上看到余额变化
            _spawn(refresh_account_balance(account, force=True), label="balance-after-success")
            append_log(task, f"完成，得到 {len(task['outputs'])} 个结果")

            # 填了去水印应用就把视频再处理一遍；这一步失败不影响原结果
            if str(canvas.get("polishAppId") or "").strip() and not task.get("cancelRequested"):
                task["status"] = "polishing"
                task["message"] = "去水印处理中…"
                store.mark_dirty("tasks")
                store.save()
                try:
                    await polish_video_outputs(task, canvas, account)
                except Exception as err:  # noqa: BLE001
                    if task.get("cancelRequested"):
                        task["status"] = "canceled"
                        task["message"] = "已取消"
                        task["finishedAt"] = now_ms()
                        store.mark_dirty("tasks")
                        store.save()
                        return
                    append_log(task, f"去水印失败，已保留原视频：{err}")

            task["status"] = "success"
            task["message"] = "已完成"
            task["finishedAt"] = now_ms()
            return
        else:
            # 平台明确判定这个任务失败了，这种才允许换账号重发（definitive 标记）
            err = RuntimeError(result.get("error") or "任务执行失败")
            setattr(err, "definitive", True)
            raise err

        store.mark_dirty("tasks")
        store.save()


async def handle_failure(task: dict, canvas: dict, account: dict, err: Exception) -> str:
    raw_message = str(err)
    balance_rejected = is_balance_short(raw_message)
    message = raw_message
    if balance_rejected:
        message = balance_short_text(account)
        # 平台说这个号扣不起这次任务：短期内别再把尝试次数烧在它身上（反正一样被拒），
        # 让重试去试别的账号。余额比被拒时变多（充值 / 领币后回查）就自动解除。
        await mark_balance_rejected(account)

    # ① 平台说这个账号并发已满：是「账号忙」，不是任务失败。
    #    不计失败次数、不报错，让账号歇一会儿，任务照常排队等它（或等别的空闲账号）。
    if is_queue_maxed(message) and not task.get("remoteTaskId"):
        task["attempts"] = max(0, int(task.get("attempts") or 0) - 1)
        account["queueFullUntil"] = now_ms() + QUEUE_FULL_COOLDOWN_MS
        task["status"] = "queued"
        task["message"] = f"账号「{account.get('name')}」并发已满，稍后自动重试"
        task["accountId"] = None
        task["accountName"] = None
        append_log(task, f"平台提示账号并发已满（TASK_QUEUE_MAXED），不计失败，{round(QUEUE_FULL_COOLDOWN_MS / 1000)} 秒后再试")
        store.mark_dirty("tasks")
        store.mark_dirty("accounts")
        store.save()
        return "queue_maxed"

    # ② 任务已经提交到平台、但只是连接不稳/查不到：**绝不重发**。
    #    重发 = 重复扣费 + 把平台队列顶爆（TASK_QUEUE_MAXED 就是这么来的）。
    #    保留远端任务号，隔一会儿回来接着等，结果照样能拿回来。
    if task.get("remoteTaskId") and not getattr(err, "fatal", False) and not getattr(err, "definitive", False):
        task["status"] = "remote_queued"
        task["message"] = "与平台连接不稳定，稍后继续等待结果"
        task["nextResumeAt"] = now_ms() + RESUME_BACKOFF_MS
        append_log(task, "连接异常，但任务已在平台侧，保留远端任务号继续等待（不会重发）")
        store.mark_dirty("tasks")
        store.save()
        return "resume"

    task["error"] = message
    append_log(task, f"失败：{message}")

    can_retry = (
        not getattr(err, "fatal", False)
        and (canvas or {}).get("autoRetry") is not False
        and int(task.get("attempts") or 0) < int(task.get("maxAttempts") or 1)
        and not task.get("cancelRequested")
    )

    if can_retry:
        task["status"] = "queued"
        task["message"] = "失败，自动重试中"
        task["accountId"] = None
        task["accountName"] = None
        task["remoteTaskId"] = None
        task["pinnedAccountId"] = None  # 换一个账号重试
        # 余额被拒 + 画布绑定的就是这个号：光清 pinnedAccountId 不够（画布绑定优先级更高，
        # 重试还会抓着同一个号撞第二次，实测就是 2 次尝试全烧在同一个账号上）。
        # 这里明确告诉调度循环：这次重试无视画布绑定，去试其它账号。
        if balance_rejected and (canvas or {}).get("accountId") == account.get("id"):
            task["ignoreCanvasPin"] = True
            append_log(task, f"画布绑定的账号「{account.get('name')}」余额被拒，重试将改用其它账号（不浪费尝试次数）")
        append_log(task, "已重新排队，将换用其它空闲账号重试")
        store.mark_dirty("tasks")
        store.save()
        return "retry"

    account.setdefault("stats", {})
    account["stats"]["failed"] = int(account["stats"].get("failed") or 0) + 1
    task["status"] = "canceled" if task.get("cancelRequested") else "failed"
    task["message"] = "已取消" if task.get("cancelRequested") else message
    task["finishedAt"] = now_ms()
    store.mark_dirty("tasks")
    store.mark_dirty("accounts")
    store.save()
    return "failed"


# ============================================================ 手动重查结果


async def refresh_task_result(task: dict) -> dict:
    """手动重查一个已提交任务在平台侧的最新状态与结果。

    用途：任务在本地被判失败 / 丢了结果，但远端其实已经跑完 —— 这里按 remoteTaskId 取回来并下载到本地。
    只查、只下载：**绝不重新提交、不改扣费额度、不动尝试次数**。
    """
    if not task.get("remoteTaskId"):
        raise ValueError("这个任务没有远端任务号（可能从未提交成功），无法重查")
    account = find_account(task.get("accountId"))
    if not account:
        raise ValueError("原账号已删除，无法用它的 Key 查询")
    canvas = find_canvas(task.get("canvasId"))

    result = await rh.query_outputs(
        base_url=api_base_url(account, canvas), api_key=account["apiKey"], task_id=str(task["remoteTaskId"])
    )
    if result.get("state") == "success":
        task["outputs"] = await download_outputs(task, result.get("outputs") or [])
        task["status"] = "success"
        task["message"] = "已完成（重查）"
        task["error"] = None
        task["finishedAt"] = now_ms()
        append_log(task, f"重查拿到 {len(task['outputs'])} 个结果")
    elif result.get("state") == "running":
        task["status"] = "running"
        task["message"] = "生成中（重查）"
    elif result.get("state") == "queued":
        task["status"] = "remote_queued"
        task["message"] = "排队中（重查）"
    else:
        append_log(task, f"重查：平台侧仍是失败状态（{result.get('error') or '未知'}）")
    store.mark_dirty("tasks")
    store.save()
    store.flush()
    return task


# ============================================================ 按任务号取回 / 一键取回


def _today_folder() -> str:
    """当天日期文件夹名，如 2026-10-11"""
    import datetime

    d = datetime.date.today()
    return f"{d.year:04d}-{d.month:02d}-{d.day:02d}"


def outputs_on_disk(task: dict) -> bool:
    """这条任务的结果文件是不是都还在磁盘上；没结果、或文件被删了，都算「缺」"""
    outs = task.get("outputs") if isinstance(task.get("outputs"), list) else []
    if not outs:
        return False
    return all(o.get("localPath") and Path(str(o["localPath"])).exists() for o in outs)


async def import_remote_task(
    *, remote_task_id: Any = None, account_id: Any = None, canvas_id: Any = None
) -> dict:
    """按平台任务号取回一条任务（原版 `importRemoteTask`）。

    为什么不能只做「查一下」：用户手上有平台侧的任务号（平台网页里能看见），
    本地可能根本没记录（比如换机器、库被清过）。所以要能凭任务号造一条本地任务，
    再用它的 remoteTaskId 去平台取结果。取回失败时必须把它标成 failed ——
    'pending' 不在 PENDING_STATES 里，调度器和孤儿回收都不会捡它，
    列表里会永远挂着一条僵尸记录。
    """
    task_id = str(remote_task_id or "").strip()
    if not re.match(r"^\d{6,}$", task_id):
        raise ValueError("请填写平台上的任务号（一串数字，如 2105984078233952258）")
    account = find_account(account_id)
    if not account:
        raise ValueError("请选择一个账号 —— 要用它的 API Key 去平台取回")

    # 同一个远端任务号已经有记录：直接重查那条，别再建一条重复的
    existing = next((t for t in store.tasks if str(t.get("remoteTaskId") or "") == task_id), None)
    if existing:
        result = await refresh_task_result(existing)
        return {**result, "reused": True}

    canvas = find_canvas(canvas_id)
    now = now_ms()
    task = {
        "id": next_id("tk"),
        "canvasId": (canvas or {}).get("id") or "",
        "canvasName": (canvas or {}).get("name") or "（手动取回）",
        "mode": "app",
        "status": "pending",
        "message": "正在向平台取回…",
        "attempts": 1,
        "maxAttempts": 1,
        "accountId": account["id"],
        "accountName": account["name"],
        "pinnedAccountId": None,
        "remoteTaskId": task_id,
        "outputs": [],
        "error": "",
        "log": [],
        "createdAt": now,
        "startedAt": now,
        "finishedAt": 0,
        # 标记来源：这条不是程序提交的，是用户按任务号补回来的
        "imported": True,
        "cancelRequested": False,
        "nextResumeAt": 0,
    }
    append_log(task, f"按任务号取回平台任务 {task_id}（账号「{account['name']}」）")
    store.tasks.insert(0, task)
    store.mark_dirty("tasks")
    store.save()
    store.flush()

    try:
        result = await refresh_task_result(task)
        return {**result, "reused": False}
    except Exception as err:  # noqa: BLE001 - 原版也是 catch 一切
        # 取回失败（网络 / Key 问题）时不能让这条任务停在 'pending'：
        # 它不在 PENDING_STATES 里，调度器和孤儿任务回收都不会捡它，
        # 于是列表里永远挂着一条「正在向平台取回…」的僵尸记录，
        # 点重试又会因为 canvasId 是空的报「画布已被删除」。
        # 标成失败并记清原因，用户还能用「重查结果」再试。
        task["status"] = "failed"
        task["error"] = str(err)
        task["message"] = f"取回失败：{task['error']}"
        task["finishedAt"] = now_ms()
        append_log(task, f"按任务号取回失败：{task['error']}")
        store.mark_dirty("tasks")
        store.save()
        store.flush()
        raise


async def fetch_missing_results() -> dict:
    """「一键取回所有结果」：把本地结果文件已经不在磁盘上的任务，按远端任务号重新取一遍。

    只查不重发，不重新扣费；已经好好的任务不动。一条一条来、每条都落盘，
    中途关掉软件也不会丢已经取回的。
    """
    folder = _today_folder()
    candidates = [t for t in store.tasks if t.get("remoteTaskId") and not outputs_on_disk(t)]
    summary = {
        "folder": folder,
        "total": len(candidates),
        "recovered": 0,
        "stillRunning": 0,
        "failed": 0,
        "skipped": 0,
        "errors": [],
    }

    for task in candidates:
        account = find_account(task.get("accountId"))
        if not account:
            summary["skipped"] += 1
            summary["errors"].append(f"{task['id']}：原账号已删除，取不回")
            continue
        canvas = find_canvas(task.get("canvasId"))
        base_url = api_base_url(account, canvas)
        try:
            result = await rh.query_outputs(
                base_url=base_url, api_key=account["apiKey"], task_id=str(task["remoteTaskId"])
            )
            state = result.get("state")
            if state == "success":
                saved = await download_outputs(task, result.get("outputs") or [], "", folder)
                got = len([o for o in saved if o.get("localUrl")])
                if got:
                    task["outputs"] = saved
                    task["status"] = "success"
                    task["message"] = "已完成"
                    task["error"] = None
                    task["finishedAt"] = task.get("finishedAt") or now_ms()
                    append_log(task, f"一键取回：取回 {got} 个结果，存到 {folder}/")
                    summary["recovered"] += 1
                else:
                    append_log(task, "一键取回：平台已完成，但结果没下下来")
                    summary["failed"] += 1
            elif state in ("running", "queued"):
                label = "仍在生成中" if state == "running" else "排队中"
                append_log(task, f"一键取回：平台{label}")
                summary["stillRunning"] += 1
            else:
                # 平台明确失败：本地要是已经成功过就别改写回失败
                if task.get("status") != "success":
                    task["status"] = "failed"
                    task["error"] = result.get("error") or "平台返回失败"
                    task["finishedAt"] = task.get("finishedAt") or now_ms()
                append_log(task, f"一键取回：平台判定失败 —— {result.get('error') or '未知原因'}")
                summary["failed"] += 1
        except Exception as err:  # noqa: BLE001
            summary["errors"].append(f"{task['id']}：{err}")
            summary["failed"] += 1
        store.mark_dirty("tasks")
        store.save()
        store.flush()

    return summary
