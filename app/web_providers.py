"""网页平台（Web Provider）的声明式配置 —— 复刻 `server/web-providers.js`。

以前这些平台是写死在 Electron 侧的常量里的：加一家要改两处源码、重新打包，
用户自己加不了。现在搬进库里 —— 「接一家平台」变成填一份配置
（页面地址 + 选择器 + 额度规则），界面上就能增删改，不用动代码。

一份配置长这样::

    {slug, name, url, hosts, loginUrl, hint, enabled,
     adapter: {input: [选择器...], submit: [...], submitText: '正则源码', result: [...]},
     quota:   {unit, daily, cost, note}}

选择器为什么是「候选数组」而不是一个：这些平台全是 SPA，改版是常态。
每格填 2~4 个候选，按顺序试，第一个命中的就用；全不命中也不报错，
界面会引导用户手动操作（或直接在平台页点下载，下载通道是兜底）。

`submitText` 是**正则的源码字符串**（存在 JSON 里，正则对象没法序列化），
注入页面世界时再编译回来。
"""

from __future__ import annotations

import copy
import random
import re
import time

#: 内置平台。
#:
#: 前四家（豆包 / 千问 / 元宝 / 智谱清言）是「cookie 型」：账号是网页登录态，
#: 每天有免费额度，多绑几个账号额度就叠加 —— 这是多账户额度池的用武之地。
#: 后面几家沿用原先内嵌浏览器那套。
#:
#: 注意：这里的 URL 与选择器是**首版候选**，平台随时改版。
#: 失效了不用等发新版：在「平台管理」里把选择器改成页面上真实的那个就行。
BUILTIN_PROVIDERS: list[dict] = [
    {
        "slug": "doubao",
        "name": "豆包",
        "url": "https://www.doubao.com/chat/",
        "hosts": ["doubao.com"],
        "loginUrl": "https://www.doubao.com/chat/",
        "hint": "网页版豆包。登录后可绑多个账号，每天各有免费生成额度",
        "adapter": {
            "input": [
                'div[contenteditable="true"]',
                "textarea[placeholder]",
                "textarea",
                'div[role="textbox"]',
            ],
            "submit": ['button[type="submit"]', 'button[aria-label*="发送"]', "button"],
            "submitText": "发送|生成|submit|send",
            "result": ['img[src*="doubao"]', 'video[src*="doubao"]', 'img[src^="http"]'],
        },
        # 每日免费额度按账号算，各账号独立记账；daily 填 0 = 不限制（还没摸清就先不设限）
        "quota": {"unit": "次", "daily": 0, "cost": 1, "note": "免费额度以豆包页面实际显示为准，可在平台管理里调整"},
    },
    {
        "slug": "qwen",
        "name": "千问 · 通义万相",
        "url": "https://tongyi.aliyun.com/wanxiang/",
        "hosts": ["tongyi.aliyun.com", "qianwen.com"],
        "loginUrl": "https://tongyi.aliyun.com/wanxiang/",
        "hint": "阿里通义万相。网页自带风控签名，不用逆向，靠页面自己算",
        "adapter": {
            "input": ["textarea", 'div[contenteditable="true"]', 'input[type="text"]'],
            "submit": ['button[type="submit"]', "button"],
            "submitText": "生成|立即生成|generate",
            "result": ['img[src*="aliyun"]', 'video[src*="aliyun"]', 'img[src^="http"]'],
        },
        "quota": {"unit": "次", "daily": 0, "cost": 1, "note": ""},
    },
    {
        "slug": "yuanbao",
        "name": "元宝 · 混元",
        "url": "https://yuanbao.tencent.com/chat",
        "hosts": ["yuanbao.tencent.com"],
        "loginUrl": "https://yuanbao.tencent.com/chat",
        "hint": "腾讯元宝。固定次数免费额度",
        "adapter": {
            "input": ['div[contenteditable="true"]', "textarea", 'input[type="text"]'],
            "submit": ['button[type="submit"]', "button"],
            "submitText": "发送|生成|submit|send",
            "result": ['img[src*="yuanbao"]', 'video[src*="yuanbao"]', 'img[src^="http"]'],
        },
        "quota": {"unit": "次", "daily": 0, "cost": 1, "note": ""},
    },
    {
        "slug": "chatglm",
        "name": "智谱清言",
        "url": "https://chatglm.cn/main/alltoolsdetail",
        "hosts": ["chatglm.cn"],
        "loginUrl": "https://chatglm.cn/",
        "hint": "智谱清言网页版",
        "adapter": {
            "input": ["textarea", 'div[contenteditable="true"]'],
            "submit": ['button[type="submit"]', "button"],
            "submitText": "生成|发送|submit|send",
            "result": ['img[src*="chatglm"]', 'img[src^="http"]'],
        },
        "quota": {"unit": "次", "daily": 0, "cost": 1, "note": ""},
    },
    {
        "slug": "ideogram",
        "name": "Ideogram",
        "url": "https://ideogram.ai/",
        "hosts": ["ideogram.ai"],
        "hint": "英文界面，可用模块里的「整页翻译」",
        "adapter": {
            "input": ["textarea", 'input[type="text"]', 'div[contenteditable="true"]'],
            "submit": ['button[type="submit"]', "button"],
            "submitText": "generate|生成",
            "result": ['img[src*="ideogram"]', 'img[src*="cloudfront"]', 'img[src^="http"]'],
        },
        "quota": {"unit": "次", "daily": 0, "cost": 1, "note": ""},
    },
    {
        "slug": "leonardo",
        "name": "Leonardo",
        "url": "https://leonardo.ai/",
        "hosts": ["leonardo.ai"],
        "hint": "英文界面，可用模块里的「整页翻译」",
        "adapter": {
            "input": ["textarea[placeholder]", "textarea", 'div[contenteditable="true"]'],
            "submit": ['button[type="submit"]', "form button", "button"],
            "submitText": "generate|generate image|生成|create",
            "result": ['img[src*="cdn.leonardo"]', 'img[src*="leonardo"]', 'img[src^="http"]'],
        },
        "quota": {"unit": "次", "daily": 0, "cost": 1, "note": ""},
    },
    {
        "slug": "copilot",
        "name": "Bing 图像 · Copilot",
        "url": "https://copilot.microsoft.com/",
        "hosts": ["copilot.microsoft.com", "bing.com", "bingusercontent.com", "microsoft.com"],
        "hint": "",
        "adapter": {
            "input": ["textarea#userInput", "textarea", 'div[contenteditable="true"]'],
            "submit": [
                'button[aria-label*="Submit"]',
                'button[title*="Send"]',
                'button[type="submit"]',
                "button",
            ],
            "submitText": "send|发送|提交|生成",
            "result": [
                'img[src*="bingusercontent"]',
                'img[src*="th.bing"]',
                'img[src*="copilot"]',
                'img[src^="http"]',
            ],
        },
        "quota": {"unit": "次", "daily": 0, "cost": 1, "note": ""},
    },
    {
        "slug": "shenlang-canvas",
        "name": "沈浪超级Ai画布（独家版）",
        "url": "https://www.yuque.com/vip99180/rg39r3/anfv8pm0ghwzq87k",
        "hosts": ["yuque.com", "www.yuque.com"],
        "hint": "官方页：打开后下载「沈浪超级Ai画布 Setup」Windows 版；这是独立应用，不是在线生图平台",
        "adapter": {"input": [], "submit": [], "submitText": "", "result": []},
        "quota": {"unit": "次", "daily": 0, "cost": 1, "note": ""},
    },
]


def partition_of(slug: str) -> str:
    """分区名：一个平台（或平台下的一个账号）一个，登录态互不干扰"""
    return f"persist:gen-{slug}"


def _str(value, fallback: str = "") -> str:
    return fallback if value is None else str(value)


def sanitize(raw: dict | None, base: dict | None = None) -> dict:
    """这份配置里的字段白名单：前端传什么都只认这些，避免把乱七八糟的东西写进库"""
    raw = raw or {}
    out = dict(base or {})

    out["slug"] = _str(raw.get("slug") or out.get("slug"), "").strip()
    out["name"] = _str(raw.get("name") or out.get("name"), "").strip()
    out["url"] = _str(raw.get("url") or out.get("url"), "").strip()
    out["loginUrl"] = _str(raw.get("loginUrl") or out.get("loginUrl"), "").strip()
    out["hint"] = _str(raw.get("hint") or out.get("hint"), "")
    if "enabled" in raw and raw["enabled"] is not None:
        out["enabled"] = bool(raw["enabled"])
    else:
        out["enabled"] = out.get("enabled", True) is not False

    if isinstance(raw.get("hosts"), (list, tuple)):
        out["hosts"] = [str(h or "").strip() for h in raw["hosts"] if str(h or "").strip()][:20]
    else:
        out["hosts"] = list(out.get("hosts") or [])

    def _list(value) -> list[str]:
        if not isinstance(value, (list, tuple)):
            return []
        return [str(s or "").strip() for s in value if str(s or "").strip()][:10]

    adapter = raw.get("adapter") or {}
    prev_adapter = out.get("adapter") or {}
    # 正则源码：用户可能手滑写个不合法的，存之前先试编译一次，不合法就当没有
    submit_text = _str(adapter.get("submitText", prev_adapter.get("submitText")), "").strip()
    if submit_text:
        try:
            re.compile(submit_text, re.I)
        except re.error:
            submit_text = ""
    out["adapter"] = {
        "input": _list(adapter.get("input")),
        "submit": _list(adapter.get("submit")),
        "submitText": submit_text,
        "result": _list(adapter.get("result")),
    }

    quota = raw.get("quota") or {}
    prev_quota = out.get("quota") or {}
    daily_raw = quota.get("daily", prev_quota.get("daily", 0))
    cost_raw = quota.get("cost", prev_quota.get("cost", 1))
    try:
        daily = max(0, int(float(daily_raw or 0)))
    except (TypeError, ValueError):
        daily = 0
    try:
        cost = max(1, int(float(cost_raw or 1)))
    except (TypeError, ValueError):
        cost = 1
    out["quota"] = {
        "unit": _str(quota.get("unit") or prev_quota.get("unit") or "次"),
        "daily": daily,
        "cost": cost,
        "note": _str(quota.get("note", prev_quota.get("note")), ""),
    }
    return out


def builtin_copy() -> list[dict]:
    """内置清单的一份深拷贝（seed 用，免得把模块常量改脏）"""
    return [sanitize(copy.deepcopy(p), copy.deepcopy(p)) for p in BUILTIN_PROVIDERS]


def ensure_providers() -> list[dict]:
    """把配置表挂到 store 上并确保有内容。

    首次运行 seed 内置清单；以后用户改过的以库里的为准 ——
    内置的只作为「新增平台」时的模板，不再覆盖用户数据。
    """
    from .store import store

    if not store.web_providers:
        store.replace("web_providers", builtin_copy())
        store.save()
        return store.web_providers
    return store.web_providers


def list_providers() -> list[dict]:
    from .store import store

    return [{**p, "partition": partition_of(p.get("slug") or "")} for p in (store.web_providers or [])]


def find_provider(slug: str | None) -> dict | None:
    """按 slug 找（内置平台和「这家配了什么」都用它）"""
    from .store import store

    if not slug:
        return None
    for p in store.web_providers or []:
        if p.get("slug") == slug:
            return p
    return None


def find_provider_by_id(provider_id: str | None) -> dict | None:
    """按 id 找：更新时前端只回传 id，不回 slug，得能按 id 定位到原来那份"""
    from .store import store

    if not provider_id:
        return None
    for p in store.web_providers or []:
        if p.get("id") == provider_id:
            return p
    return None


def _new_provider_id() -> str:
    now = int(time.time() * 1000)
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out, value = "", now
    while value:
        value, rem = divmod(value, 36)
        out = digits[rem] + out
    rand = "".join(random.choice(digits) for _ in range(4))
    return f"wp-{out}-{rand}"


SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9\-_]{0,31}$", re.I)


def upsert_provider(raw: dict | None) -> dict:
    """新增或更新；返回 `{provider, created}`，slug 冲突/为空时抛错"""
    from .store import store

    raw = raw or {}
    rows = store.web_providers
    base = find_provider_by_id(raw.get("id")) if raw.get("id") else None
    payload = sanitize(raw, copy.deepcopy(base) if base else {})

    if not payload.get("slug"):
        raise ValueError("请填写平台标识（slug）")
    if not SLUG_RE.match(payload["slug"]):
        raise ValueError("平台标识只能用字母、数字、短横线和下划线（如 doubao-2）")
    if not payload.get("name"):
        raise ValueError("请填写平台名称")

    by_id = -1
    if raw.get("id"):
        for index, p in enumerate(rows):
            if p.get("id") == raw["id"]:
                by_id = index
                break

    if by_id >= 0:
        # 改 slug 时不能和别人撞
        clash = any(i != by_id and p.get("slug") == payload["slug"] for i, p in enumerate(rows))
        if clash:
            raise ValueError(f"已经有平台用了标识「{payload['slug']}」")
        prev = rows[by_id]
        rows[by_id] = {**prev, **payload, "id": prev.get("id")}
        store.mark_dirty("web_providers")
        store.save()
        return {"provider": rows[by_id], "created": False}

    for p in rows:
        if p.get("slug") == payload["slug"]:
            raise ValueError(f"已经有平台用了标识「{payload['slug']}」")

    provider = {**payload, "id": _new_provider_id()}
    rows.append(provider)
    store.mark_dirty("web_providers")
    store.save()
    return {"provider": provider, "created": True}


def delete_provider(provider_id: str) -> bool:
    from .store import store

    rows = store.web_providers
    for index, p in enumerate(rows):
        if p.get("id") == provider_id or p.get("slug") == provider_id:
            del rows[index]
            store.mark_dirty("web_providers")
            store.save()
            return True
    return False


def reset_providers() -> int:
    """恢复内置清单：只补「库里没有的」，用户改过的那份保持原样"""
    from .store import store

    rows = store.web_providers
    have = {p.get("slug") for p in rows}
    added = 0
    for p in builtin_copy():
        if p.get("slug") in have:
            continue
        p["id"] = _new_provider_id()
        rows.append(p)
        added += 1
    if added:
        store.mark_dirty("web_providers")
        store.save()
    return added
