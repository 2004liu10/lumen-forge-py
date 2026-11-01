"""ID 生成与时间工具。

对应 Node 版 `store.js:585-587`：

    export function nextId(prefix) {
      return `${prefix}_${Date.now().toString(36)}${Math.random().toString(36).slice(2, 7)}`;
    }

Python 版保持同样的形态（前缀 + 36 进制时间戳 + 5 位随机），
这样老 db.json 里的 ID 和新生成的 ID 在格式上完全一致，
前端任何按前缀判断类型的逻辑都不会被打断。
"""

from __future__ import annotations

import random
import string
import time
from datetime import datetime, timezone


_ALPHABET = string.digits + string.ascii_lowercase


def _to_base36(num: int) -> str:
    if num == 0:
        return "0"
    out = []
    while num:
        num, rem = divmod(num, 36)
        out.append(_ALPHABET[rem])
    return "".join(reversed(out))


def next_id(prefix: str) -> str:
    """生成 `prefix_xxxxx` 形式的 ID，格式与 Node 版一致"""
    ts = _to_base36(int(time.time() * 1000))
    rand = "".join(random.choices(_ALPHABET, k=5))
    return f"{prefix}_{ts}{rand}"


def now_ms() -> int:
    """当前毫秒时间戳（Node 版全库统一用 Date.now()，保持同单位）"""
    return int(time.time() * 1000)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def today_key(ts_ms: int | None = None) -> str:
    """本地日期键 YYYY-MM-DD（对应 web-accounts.js:todayKey，用于网页账号每日额度）"""
    t = datetime.fromtimestamp((ts_ms or now_ms()) / 1000)
    return t.strftime("%Y-%m-%d")
