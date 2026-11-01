"""话题库 —— 复刻 `server/topics.js`（3653 字节）。

## 解决的实际问题

用户在「素材 → 抖音采集」里刷到一条视频，页面上明明带着话题，
但把视频加进画布之后话题就没了 —— 等生成出自己的视频、要去发布时，
那些话题早找不到了。

所以这里把话题单独攒成一份库：加素材时自动收进来，发布时挑着用。
**话题不绑视频**（同一批话题可以跨作品复用），只记「谁带来的、用了几次」。
"""

from __future__ import annotations

import re
import time

from .ids import now_ms
from .models import new_topic
from .store import store

#: 一条话题最多 30 字，超出直接截断（抖音本身也有限制）
MAX_NAME = 30
#: 一次最多收 12 个（和 preload 那边的上限对齐）
MAX_PER_CALL = 12


def _norm(name) -> str:
    """去前导 # → 去所有空白 → 截断到 30 字"""
    return re.sub(r"\s+", "", re.sub(r"^#+", "", str(name or "")).strip())[:MAX_NAME]


def find_topic(topic_id: str | None) -> dict | None:
    if not topic_id:
        return None
    return store.find("topic_library", id=topic_id)


def add_topics(names, *, source: str = "douyin", source_desc: str = "") -> dict:
    """收一批话题进库。已存在的只更新来源与时间，不重复建。

    返回 `{added, existed, items}`：`added` 是这次新收进来的名字，方便界面提示。

    调用方（见 `api/topics.py`）会传 `names || name`，单条补话题时传进来的是字符串。
    原版以前这里只认数组，字符串直接被归零 —— 返回 200 却一个都没收进去，界面毫无提示。
    这里统一成数组。
    """
    if isinstance(names, (list, tuple)):
        raw = list(names)
    elif names is None:
        raw = []
    else:
        raw = [names]

    wanted = [n for n in (_norm(x) for x in raw) if n][:MAX_PER_CALL]
    added: list[str] = []
    existed: list[str] = []
    items: list[dict] = []

    for name in wanted:
        hit = store.find("topic_library", name=name)
        if hit:
            # 已经有的：补一下来源描述（留着最近一次的出处，方便回想）
            if source_desc and not hit.get("sourceDesc"):
                hit["sourceDesc"] = str(source_desc)[:80]
            existed.append(name)
            items.append(hit)
            continue
        topic = new_topic(name=name, source=source, source_desc=source_desc)
        store.topic_library.append(topic)
        added.append(name)
        items.append(topic)

    if added or existed:
        store.mark_dirty("topic_library")
        store.save()
        store.flush()
    return {"added": added, "existed": existed, "items": items}


def mark_topics_used(names) -> int:
    """记一次「用过」：发布成功后调，用过的排前面"""
    wanted = [n for n in (_norm(x) for x in (names if isinstance(names, (list, tuple)) else [])) if n]
    if not wanted:
        return 0
    touched = 0
    for name in wanted:
        hit = store.find("topic_library", name=name)
        if not hit:
            continue
        hit["usedCount"] = (int(hit.get("usedCount") or 0)) + 1
        hit["lastUsedAt"] = now_ms()
        touched += 1
    if touched:
        store.mark_dirty("topic_library")
        store.save()
        store.flush()
    return touched


def remove_topic(topic: dict) -> None:
    rows = store.topic_library
    for index, item in enumerate(rows):
        if item.get("id") == topic.get("id"):
            del rows[index]
            break
    store.mark_dirty("topic_library")
    store.save()
    store.flush()


def clear_topics() -> int:
    count = len(store.topic_library)
    store.replace("topic_library", [])
    store.save()
    store.flush()
    return count


def topic_list(*, q: str = "") -> list[dict]:
    """给界面的话题列表。

    排序：用过的（按最近使用）排前面，没用过的按收录时间倒序 ——
    常用话题会自然浮到顶上，挑的时候不用翻。
    """
    keyword = _norm(q)
    rows = list(store.topic_library)
    if keyword:
        rows = [t for t in rows if keyword in str(t.get("name") or "")]

    def sort_key(t: dict):
        used = int(t.get("usedCount") or 0)
        if used > 0:
            # 用过的：按最近使用降序，且永远排在没用过的前面
            return (0, -int(t.get("lastUsedAt") or 0))
        return (1, -int(t.get("createdAt") or 0))

    rows.sort(key=sort_key)
    return [
        {
            "id": t.get("id"),
            "name": t.get("name"),
            "source": t.get("source"),
            "sourceDesc": t.get("sourceDesc") or "",
            "usedCount": int(t.get("usedCount") or 0),
            "lastUsedAt": t.get("lastUsedAt") or 0,
            "createdAt": t.get("createdAt"),
        }
        for t in rows
    ]


def topic_stats() -> dict:
    """话题库统计（界面上显示「共 N 个」）"""
    rows = store.topic_library
    return {
        "total": len(rows),
        "used": sum(1 for t in rows if (int(t.get("usedCount") or 0)) > 0),
    }
