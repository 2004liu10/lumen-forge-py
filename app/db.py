"""数据层：SQLite 存储 + 原 db.json 迁移。

设计要点（与原 Node 版 store.js 的差异都记在这里）：

1. **表结构**：原版 db.json 是 20 张「表」+ 30 多个一次性迁移标记。
   这里把每张表落成独立 SQLite 表，`payload` 列存 JSON ——
   即「宽表 + JSON 列」。原因：原版的字段是随功能演进不断加的（比如
   accounts 上有 balanceRejectedCoins / loginPass 这类后来才加的字段），
   逐字段建列会让每次功能演进都要写 ALTER TABLE 迁移。
   用 JSON 列可以在 Python 侧保持和 JS 一样的「字典随手加键」语义。

2. **保留的是原版的裁剪规则**：store.js:505-534 在每次 flush 时会裁剪
   tasks 保留 400、imageTasks 60、videoTasks 60、storyboards 50、
   topicLibrary 300、publishLog 200。这些规则照搬，否则库会无限膨胀。

3. **写盘策略**：原版是 250ms 防抖 + 全量重写 db.json。
   这里用 SQLite 事务 + WAL，单行 upsert，不再全量重写。
   这是换 SQLite 最主要的收益 —— 596KB 的 JSON 全量重写会随数据增长变成瓶颈。

4. **密钥加密**：写入前走 secret.seal_row，读出后走 secret.open_row，
   与 Node 版「内存明文 / 磁盘密文」的约定一致。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

from .config import (
    DB_FILE,
    KEEP_IMAGE_TASKS,
    KEEP_PUBLISH_LOG,
    KEEP_STORYBOARDS,
    KEEP_TASKS,
    KEEP_TOPICS,
    KEEP_VIDEO_TASKS,
    LEGACY_DB_FILE,
    MAX_IMAGE_REFS,
)
from .ids import now_ms
from .secret import open_row, seal_row

log = logging.getLogger(__name__)

# 所有表清单：与原 db.json 的顶层键一一对应
# 列表型表（每行一条记录，有 id）
LIST_TABLES = [
    "accounts",
    "canvases",
    "tasks",
    "image_models",
    "image_tasks",
    "image_refs",
    "video_models",
    "video_tasks",
    "video_inputs",
    "storyboards",
    "material_sources",
    "node_canvases",
    "node_assets",
    "topic_library",
    "publish_log",
    "web_accounts",
    "web_providers",
    "topaz_jobs",
]
# 单例型表（整张表只有一个对象，无 id）
SINGLETON_TABLES = [
    "storage",
    "topaz_local",
    "discover",
    "settings",
    "tag_model",
    "story_model",
    "material_line_pool",
]


class Store:
    """SQLite 数据仓库。线程安全（调度器线程与 HTTP 线程会并发访问）。"""

    def __init__(self, db_file: Path = DB_FILE) -> None:
        self.db_file = Path(db_file)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_file), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # WAL：读写不互相阻塞，这对「调度器在写、HTTP 在读」的场景很关键
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=OFF")
        self._create_schema()

    # ------------------------------------------------------------------ 建表

    def _create_schema(self) -> None:
        with self._lock, self._conn:
            for table in LIST_TABLES:
                self._conn.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {table} (
                        id          TEXT PRIMARY KEY,
                        payload     TEXT NOT NULL,
                        created_at  INTEGER NOT NULL DEFAULT 0,
                        updated_at  INTEGER NOT NULL DEFAULT 0
                    )
                    """
                )
                self._conn.execute(
                    f"CREATE INDEX IF NOT EXISTS idx_{table}_created ON {table}(created_at DESC)"
                )
            for table in SINGLETON_TABLES:
                self._conn.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {table} (
                        key     TEXT PRIMARY KEY,
                        payload TEXT NOT NULL
                    )
                    """
                )

    # ------------------------------------------------------------------ 通用

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """事务上下文：要么全成，要么全回滚"""
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -------------------------------------------------------------- 列表型表

    def list_all(self, table: str, *, limit: int | None = None) -> list[dict]:
        """读出整张表（按 created_at 倒序，与原版数组顺序语义接近）"""
        sql = f"SELECT payload FROM {table} ORDER BY created_at DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        with self._lock:
            rows = self._conn.execute(sql).fetchall()
        out = []
        for row in rows:
            item = json.loads(row["payload"])
            open_row(table, item)
            out.append(item)
        return out

    def get(self, table: str, item_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT payload FROM {table} WHERE id = ?", (item_id,)
            ).fetchone()
        if not row:
            return None
        item = json.loads(row["payload"])
        open_row(table, item)
        return item

    def upsert(self, table: str, item: dict) -> dict:
        """插入或更新一条。id 取 item['id']。"""
        item_id = item.get("id")
        if not item_id:
            raise ValueError(f"{table} 的记录缺少 id")
        created = int(item.get("createdAt") or now_ms())
        sealed = seal_row(table, item)
        payload = json.dumps(sealed, ensure_ascii=False)
        with self._lock, self._conn:
            self._conn.execute(
                f"""
                INSERT INTO {table} (id, payload, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET payload = excluded.payload,
                                              updated_at = excluded.updated_at
                """,
                (item_id, payload, created, now_ms()),
            )
        return item

    def upsert_many(self, table: str, items: Iterable[dict]) -> None:
        rows = []
        for item in items:
            item_id = item.get("id")
            if not item_id:
                continue
            created = int(item.get("createdAt") or now_ms())
            sealed = seal_row(table, item)
            rows.append((item_id, json.dumps(sealed, ensure_ascii=False), created, now_ms()))
        if not rows:
            return
        with self._lock, self._conn:
            self._conn.executemany(
                f"""
                INSERT INTO {table} (id, payload, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET payload = excluded.payload,
                                              updated_at = excluded.updated_at
                """,
                rows,
            )

    def delete(self, table: str, item_id: str) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute(f"DELETE FROM {table} WHERE id = ?", (item_id,))
        return cur.rowcount > 0

    def count(self, table: str) -> int:
        with self._lock:
            row = self._conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
        return int(row["n"]) if row else 0

    def find(self, table: str, **fields: Any) -> list[dict]:
        """按字段过滤（在 Python 侧筛，避免为每个查询建列）"""
        return [r for r in self.list_all(table) if all(r.get(k) == v for k, v in fields.items())]

    def find_one(self, table: str, **fields: Any) -> dict | None:
        for r in self.list_all(table):
            if all(r.get(k) == v for k, v in fields.items()):
                return r
        return None

    # ------------------------------------------------------------ 单例型表

    def get_singleton(self, table: str, default: Any = None) -> Any:
        with self._lock:
            row = self._conn.execute(
                f"SELECT payload FROM {table} WHERE key = 'main'"
            ).fetchone()
        if not row:
            return default
        return json.loads(row["payload"])

    def set_singleton(self, table: str, value: Any) -> None:
        payload = json.dumps(value, ensure_ascii=False)
        with self._lock, self._conn:
            self._conn.execute(
                f"""
                INSERT INTO {table} (key, payload) VALUES ('main', ?)
                ON CONFLICT(key) DO UPDATE SET payload = excluded.payload
                """,
                (payload,),
            )

    # ------------------------------------------------------------- 裁剪规则

    def trim(self) -> None:
        """按原版 flush() 的规则裁剪（store.js:505-534）"""
        rules = [
            ("tasks", KEEP_TASKS, "createdAt"),
            ("image_tasks", KEEP_IMAGE_TASKS, "createdAt"),
            ("video_tasks", KEEP_VIDEO_TASKS, "createdAt"),
            ("storyboards", KEEP_STORYBOARDS, "createdAt"),
            ("topic_library", KEEP_TOPICS, "lastUsedAt"),
            ("publish_log", KEEP_PUBLISH_LOG, "publishedAt"),
        ]
        with self._lock, self._conn:
            for table, keep, order_field in rules:
                total = self._conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
                if total <= keep:
                    continue
                # 保留最新的 keep 条，其余删掉
                self._conn.execute(
                    f"""
                    DELETE FROM {table} WHERE id NOT IN (
                        SELECT id FROM {table} ORDER BY created_at DESC LIMIT ?
                    )
                    """,
                    (keep,),
                )
                log.info("[store] 裁剪 %s：%s → %s", table, total, keep)

    # ---------------------------------------------------------------- 迁移

    def migrate_from_legacy_json(self, legacy_file: Path | None = None) -> dict[str, int]:
        """把原版 db.json 导入 SQLite。

        只在该库还是空的时候跑，避免覆盖已有数据。
        原 db.json 的键用的是驼峰（imageModels / imageTasks / nodeCanvases …），
        这里做一次映射。
        """
        path = legacy_file or LEGACY_DB_FILE
        if not path.exists():
            return {}
        # 已经有数据就不迁移
        if any(self.count(t) > 0 for t in LIST_TABLES) or self.get_singleton("settings"):
            log.info("[migrate] 目标库已有数据，跳过 db.json 导入")
            return {}

        raw = json.loads(path.read_text(encoding="utf-8"))
        stats: dict[str, int] = {}

        # 驼峰 → 下划线映射
        def snake(name: str) -> str:
            out = []
            for ch in name:
                if ch.isupper():
                    out.append("_" + ch.lower())
                else:
                    out.append(ch)
            return "".join(out)

        for key, value in raw.items():
            table = snake(key)
            if isinstance(value, list) and value and isinstance(value[0], dict):
                if table in LIST_TABLES:
                    self.upsert_many(table, value)
                    stats[table] = len(value)
            elif isinstance(value, dict):
                if table in SINGLETON_TABLES:
                    self.set_singleton(table, value)
                    stats[table] = 1
            # bool 型的一次性迁移标记（如 imageModelsSeeded）不再保留：
            # 新库是全新的，没有「要不要补种内置模型」这个历史包袱

        log.info("[migrate] 从 db.json 导入 %s 张表", len(stats))
        return stats
