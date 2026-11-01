"""数据仓库门面 —— 对齐原 Node 版 `store.js` 的 `db` 对象语义。

## 为什么要这一层

原版 `server/store.js` 暴露的是一个**内存字典** `db`：所有模块直接 `db.accounts.push(...)`、
`db.imageTasks.find(...)`，改完调 `save()`（防抖写盘）或 `flush()`（立即写盘）。
42k 行 JS 里绝大多数业务逻辑都是围绕这个模式写的。

Python 版换成 SQLite 后，如果每个模块都改成 `db.list_all("accounts")` 再自己 upsert，
那么所有业务代码都要重写一遍「读-改-写」的样板，既啰嗦又容易漏 `save()`。

所以这里保留**同一套语义**：

    store.accounts.append(account)     # 改内存
    store.save()                       # 标脏
    store.flush()                      # 落库（脏表整表 upsert + 裁剪）

这样从 JS 移植业务逻辑时，几乎可以逐行对照：`db.accounts` → `store.accounts`，
`save()` → `store.save()`，其余不变。

## 缓存与落库策略

- 表首次访问时从 SQLite 读进内存（读出的行已由 `db.py` 解密），此后读写都在内存里。
- `flush()` 只写**脏表**，且只写**内容变过的行**（按 id 比对上一次落库时的 JSON 快照），
  避免每 250ms 把整库重写一遍。
- 密钥加密在 `db.upsert` 里做，所以内存里永远是明文、磁盘上永远是密文 —— 与 Node 版一致。

## 与原版的差异

原版 `save()` 是 250ms 防抖（`setTimeout` 全量重写 db.json），`flush()` 是清掉定时器立刻写。
这里 `save()` 只标脏，`flush()` 才真正落库。调用点一一对应，所以行为等价；
但**不会有隐式写盘**：谁忘了 flush，数据就丢。为此 `trim()` 与 `flush()` 绑在一起，
和原版 flush 末尾裁剪任务的顺序一致。
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

from .config import LEGACY_DB_FILE
from .db import LIST_TABLES, SINGLETON_TABLES, Store as _SqlStore
from .ids import now_ms

log = logging.getLogger(__name__)


class Store:
    """内存字典 + SQLite 落库。属性名即表名（`store.accounts` / `store.settings`）。"""

    def __init__(self, db_file: Path | None = None) -> None:
        self._sql = _SqlStore(db_file) if db_file else _SqlStore()
        self._lock = threading.RLock()
        self._cache: dict[str, Any] = {}
        self._dirty: set[str] = set()
        # 上一次落库时每行的 JSON 快照：用来只写真正变过的行
        self._snapshot: dict[str, dict[str, str]] = {}

    # ---------------------------------------------------------- 属性访问

    def __getattr__(self, name: str) -> Any:
        # 只有表名才走缓存，其余属性（方法 / 内部字段）正常报错
        if name.startswith("_"):
            raise AttributeError(name)
        if name not in LIST_TABLES and name not in SINGLETON_TABLES:
            raise AttributeError(f"没有这张表：{name}")
        return self._table(name)

    def _table(self, name: str) -> Any:
        with self._lock:
            if name in self._cache:
                return self._cache[name]
            if name in SINGLETON_TABLES:
                # 单例表：默认空 dict（原版 `db.storage` / `db.settings` 就是对象）
                value = self._sql.get_singleton(name, {}) or {}
                self._cache[name] = value
                return value
            rows = self._sql.list_all(name)
            self._cache[name] = rows
            self._snapshot[name] = {r["id"]: self._row_json(r) for r in rows if r.get("id")}
            return rows

    @staticmethod
    def _row_json(row: dict) -> str:
        """行内容的稳定序列化（用来判断有没有变过）"""
        return json.dumps(row, ensure_ascii=False, sort_keys=True)

    # ---------------------------------------------------------- 写入语义

    def save(self) -> None:
        """标记需要落库（对应原版 `save()` 的防抖标记）

        原版 `save()` 是「250ms 后把**整个 db.json** 重写一遍」，也就是**所有表**都会落盘。
        这里必须等价：把当前已加载的所有表都标脏。

        只标一张表是不行的 —— 移植过来的代码里有 8 处只调了 `store.save()` 就改完了
        （`topics.add_topics` 之后、`node_canvas` 的若干改动、`materials` 的线路池等），
        它们在原版里能存住，靠的就是 save() 写全库。若这里只标脏「被显式 mark_dirty 的表」，
        这些调用点就会静默丢数据。

        代价可控：`flush()` → `_persist()` 会按行 JSON 快照比对，只写真正变过的行，
        所以「标脏全部」不等于「重写全部」。
        """
        with self._lock:
            self._dirty.update(self._cache.keys())

    def flush(self) -> None:
        """把脏表落库，并执行原版 flush 末尾的任务裁剪"""
        with self._lock:
            dirty = list(self._dirty)
            self._dirty.clear()
        for name in dirty:
            self._persist(name)
        if dirty:
            self._sql.trim()

    def mark_dirty(self, name: str) -> None:
        """显式标脏（有些模块改了嵌套字段，拿不到变更通知，用它）"""
        with self._lock:
            self._dirty.add(name)

    def _persist(self, name: str) -> None:
        with self._lock:
            value = self._cache.get(name)
            if value is None:
                return
            if name in SINGLETON_TABLES:
                self._sql.set_singleton(name, value)
                return
            prev = self._snapshot.setdefault(name, {})
            cur: dict[str, str] = {}
            changed: list[dict] = []
            for row in value:
                row_id = row.get("id")
                if not row_id:
                    continue
                blob = self._row_json(row)
                cur[row_id] = blob
                if prev.get(row_id) != blob:
                    changed.append(row)
            # 缓存里已经没有的行 = 被删了
            removed = [rid for rid in prev if rid not in cur]
            self._snapshot[name] = cur
        if changed:
            self._sql.upsert_many(name, changed)
        for row_id in removed:
            self._sql.delete(name, row_id)

    def replace(self, name: str, value: Any) -> None:
        """整表替换（原版有 `db.xxx = [...]` 这种写法，比如 seed 里的过滤删除）"""
        with self._lock:
            self._cache[name] = value
            self._dirty.add(name)

    def touch(self, name: str) -> None:
        """改了表内嵌套字段后调用，等价于 save() 但明确到表"""
        self.mark_dirty(name)

    # ---------------------------------------------------------- 便捷查询

    def find(self, name: str, **fields: Any) -> dict | None:
        """按字段找第一条（对应原版 `db.xxx.find((m) => m.id === id)`）"""
        for row in self._table(name):
            if all(row.get(k) == v for k, v in fields.items()):
                return row
        return None

    def filter(self, name: str, **fields: Any) -> list[dict]:
        """按字段过滤（对应原版 `db.xxx.filter(...)`）"""
        return [r for r in self._table(name) if all(r.get(k) == v for k, v in fields.items())]

    # ---------------------------------------------------------- 生命周期

    def migrate_legacy(self) -> dict[str, int]:
        """首次启动时把原 db.json 导进 SQLite（仅空库）"""
        stats = self._sql.migrate_from_legacy_json(LEGACY_DB_FILE)
        if stats:
            # 迁移完把缓存清掉，下次访问重新读
            with self._lock:
                self._cache.clear()
                self._snapshot.clear()
        return stats

    def close(self) -> None:
        self.flush()
        self._sql.close()

    # ---------------------------------------------------------- 时间工具

    @staticmethod
    def now() -> int:
        return now_ms()


#: 进程内单例。业务模块统一 `from ..store import store` 使用。
store = Store()
