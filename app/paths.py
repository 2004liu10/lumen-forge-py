"""存储位置管理 —— 逐函数复刻 `server/store.js:93-483`。

原版的 8 个媒体目录不是写死的，用户可以在「设置 → 存储位置」里改到别的盘。
改位置时还要把老目录里的文件搬过去、并把库里所有指向老目录的绝对路径一起改掉，
否则历史记录全部悬空（界面看不出问题，一用就 ENOENT）。

这里保留同一套语义，但把「模块级可变全局」改成 `Paths` 对象：
原版是 `export let GEN_DIR` 这种可以在运行时被 `applyStorageDirs()` 重写的变量，
Python 里模块级变量被 `from x import GEN_DIR` 拿走之后不会跟着变，所以必须走对象属性。
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Any

from .config import DATA_DIR

log = logging.getLogger(__name__)

#: 「设置 → 存储位置」的目录清单（对应 store.js:106-115）
STORAGE_SPECS: list[dict[str, str]] = [
    {"key": "uploads", "label": "画布素材", "desc": "上传给画布当输入的文件", "rel": "uploads"},
    {"key": "outputs", "label": "自用模块生成结果", "desc": "从 RunningHub 下载回来的成品", "rel": "outputs"},
    {
        "key": "topaz",
        "label": "画质提升结果",
        "desc": "提升画质产出的成品视频，默认在「自用模块生成结果」下的 topaz（改位置不搬旧文件）",
        "rel": "outputs/topaz",
    },
    {"key": "generated", "label": "图片生成结果", "desc": "图片生成模块产出的图", "rel": "generated"},
    {"key": "refs", "label": "参考图", "desc": "图生图的参考图，默认在「图片生成结果」下的 refs", "rel": "generated/refs"},
    {"key": "videos", "label": "视频生成结果", "desc": "图生视频产出的 mp4", "rel": "videos"},
    {"key": "videoInputs", "label": "视频输入图", "desc": "视频生成模块的输入图", "rel": "video-inputs"},
    {"key": "materials", "label": "素材下载", "desc": "素材模块从第三方接口抓下来的图片 / 视频", "rel": "materials"},
]

_SPEC_BY_KEY = {s["key"]: s for s in STORAGE_SPECS}


class Paths:
    """8 个媒体目录的当前落点。`custom` 来自 `store.storage`。"""

    def __init__(self, store: Any) -> None:
        self._store = store
        self.dirs: dict[str, Path] = {}
        self.apply()

    # ---------------------------------------------------------- 解析

    def _custom(self, key: str) -> str:
        storage = self._store.storage or {}
        return str(storage.get(key) or "").strip()

    def _pick(self, key: str, fallback: Path) -> Path:
        raw = self._custom(key)
        return Path(raw).resolve() if raw else fallback

    def resolve(self) -> dict[str, Path]:
        """算出 8 个目录当前该落在哪（对应 store.js:121-138）"""
        generated = self._pick("generated", DATA_DIR / "generated")
        outputs = self._pick("outputs", DATA_DIR / "outputs")
        return {
            "uploads": self._pick("uploads", DATA_DIR / "uploads"),
            "outputs": outputs,
            # 画质提升结果没单独设过就跟「自用模块生成结果」一起走
            "topaz": self._pick("topaz", outputs / "topaz"),
            "generated": generated,
            # 参考图没单独设过就跟「图片生成结果」一起走
            "refs": self._pick("refs", generated / "refs"),
            "videos": self._pick("videos", DATA_DIR / "videos"),
            "videoInputs": self._pick("videoInputs", DATA_DIR / "video-inputs"),
            "materials": self._pick("materials", DATA_DIR / "materials"),
        }

    def apply(self) -> dict[str, Path]:
        """把解析结果落进属性，并确保目录存在（对应 applyStorageDirs）"""
        self.dirs = self.resolve()
        for d in self.dirs.values():
            d.mkdir(parents=True, exist_ok=True)
        return self.dirs

    # ---------------------------------------------------------- 属性快捷方式

    @property
    def uploads(self) -> Path:
        return self.dirs["uploads"]

    @property
    def outputs(self) -> Path:
        return self.dirs["outputs"]

    @property
    def topaz(self) -> Path:
        return self.dirs["topaz"]

    @property
    def generated(self) -> Path:
        return self.dirs["generated"]

    @property
    def refs(self) -> Path:
        return self.dirs["refs"]

    @property
    def videos(self) -> Path:
        return self.dirs["videos"]

    @property
    def video_inputs(self) -> Path:
        return self.dirs["videoInputs"]

    @property
    def materials(self) -> Path:
        return self.dirs["materials"]

    # ---------------------------------------------------------- 对外快照

    def default_path_for(self, key: str) -> Path:
        """某个位置「恢复默认」之后会落在哪（对应 defaultPathFor）"""
        if key == "refs":
            raw = self._custom("generated")
            base = Path(raw).resolve() if raw else DATA_DIR / "generated"
            return base / "refs"
        if key == "topaz":
            raw = self._custom("outputs")
            base = Path(raw).resolve() if raw else DATA_DIR / "outputs"
            return base / "topaz"
        return DATA_DIR / _SPEC_BY_KEY[key]["rel"]

    def snapshot(self) -> list[dict[str, Any]]:
        """界面上要展示的「存储位置」清单（对应 storageSnapshot）"""
        out = []
        for spec in STORAGE_SPECS:
            key = spec["key"]
            d = self.dirs.get(key)
            files = 0
            if d and d.exists():
                try:
                    files = len(list(d.iterdir()))
                except OSError:
                    files = 0
            out.append(
                {
                    "key": key,
                    "label": spec["label"],
                    "desc": spec["desc"],
                    "path": str(d) if d else "",
                    "defaultPath": str(self.default_path_for(key)),
                    "isDefault": not self._custom(key),
                    "exists": bool(d and d.exists()),
                    "files": files,
                }
            )
        return out

    def topaz_legacy_dirs(self) -> list[Path]:
        """/files/topaz 要兼容服务的目录清单：当前目录在前，旧目录排后面"""
        legacy = (self._store.storage or {}).get("topazLegacy") or []
        return [Path(d) for d in legacy if d and Path(d).exists()]

    # ---------------------------------------------------------- 改位置

    def set_dir(self, key: str, directory: str | None) -> dict[str, Any]:
        """改某个存储位置。`directory` 传空 = 恢复默认（对应 setStorageDir）。

        会把老目录里的文件一起搬到新目录，并把库里指向老目录的路径一起改掉。
        """
        spec = _SPEC_BY_KEY.get(key)
        if not spec:
            raise ValueError("未知的存储位置")
        value = str(directory or "").strip()
        if value and not os.path.isabs(value):
            raise ValueError("请填写绝对路径，例如 D:\\我的图片")

        before = dict(self.dirs)
        storage = dict(self._store.storage or {})
        prev_custom = storage.get(key)

        def set_custom(v: str | None) -> None:
            if v:
                storage[key] = v
            else:
                storage.pop(key, None)
            self._store.replace("storage", storage)

        set_custom(value)
        after = self.resolve()
        src = before.get(key)
        dst = after.get(key)

        if src and dst and src.resolve() != dst.resolve():
            # 互相包含会自己搬自己，挡掉并把配置还原
            a = str(src.resolve()) + os.sep
            b = str(dst.resolve()) + os.sep
            if b.startswith(a) or a.startswith(b):
                set_custom(prev_custom)
                raise ValueError("新位置不能是原位置的上级或下级目录")

        changed = bool(src and dst and src.resolve() != dst.resolve())
        moved = 0
        relinked = 0

        if changed and key == "topaz":
            # 画质提升结果只改写入位置，**不搬历史文件**（成品动辄几十 GB）
            self._remember_legacy_dir(src, dst)
            self._rewrite_topaz_out_urls()
        elif changed:
            moved = self._move_dir_contents(src, dst)
            relinked = self._rewrite_local_paths(src, dst)

        self.apply()
        self._store.mark_dirty("storage")
        self._store.flush()
        return {
            "key": key,
            "label": spec["label"],
            "from": str(src) if src else "",
            "to": str(dst) if dst else "",
            "moved": moved,
            "relinked": relinked,
            "noMove": changed and key == "topaz",
        }

    def reset_dir(self, key: str) -> dict[str, Any]:
        return self.set_dir(key, "")

    def _remember_legacy_dir(self, src: Path | None, dst: Path) -> None:
        if not src:
            return
        storage = dict(self._store.storage or {})
        legacy = [Path(d).resolve() for d in (storage.get("topazLegacy") or [])]
        src_r, dst_r = src.resolve(), dst.resolve()
        if src_r != dst_r and src_r.exists() and src_r not in legacy:
            legacy.insert(0, src_r)
        storage["topazLegacy"] = [
            str(d) for d in legacy if d != dst_r and d.exists()
        ][:5]
        self._store.replace("storage", storage)

    # ---------------------------------------------------------- 路径回写

    def _rewrite_local_paths(self, from_root: Path, to_root: Path) -> int:
        """把库里指向旧目录的绝对路径改到新目录（对应 rewriteLocalPaths）"""
        src = from_root.resolve()
        dst = to_root.resolve()
        if src == dst:
            return 0

        changed = 0

        def fix(holder: Any, field: str) -> None:
            nonlocal changed
            if not isinstance(holder, dict):
                return
            value = holder.get(field)
            if not isinstance(value, str) or not value.strip():
                return
            abs_path = Path(value).resolve()
            try:
                rel = abs_path.relative_to(src)
            except ValueError:
                return
            if not rel.parts:
                return
            holder[field] = str(dst / rel)
            changed += 1

        store = self._store
        for canvas in store.canvases:
            for asset in (canvas.get("assets") or {}).values():
                fix(asset, "localPath")
        for task in store.tasks:
            for out in task.get("outputs") or []:
                fix(out, "localPath")
                fix(out.get("topaz") or {}, "sourcePath")
        for task in store.image_tasks:
            for img in task.get("images") or []:
                fix(img, "localPath")
            for ref in task.get("refs") or []:
                fix(ref, "localPath")
        for ref in store.image_refs:
            fix(ref, "localPath")
        for inp in store.video_inputs:
            fix(inp, "localPath")
        for task in store.video_tasks:
            fix(task.get("input") or {}, "localPath")
            fix(task.get("video") or {}, "localPath")
        for rec in store.publish_log:
            fix(rec, "path")
        for asset in store.node_assets:
            fix(asset, "localPath")
        for graph in store.node_canvases:
            for node in graph.get("nodes") or []:
                fix(node, "localPath")
        for item in (store.topaz_local or {}).get("results") or []:
            fix(item, "sourcePath")
            fix(item, "outPath")

        for table in ("canvases", "tasks", "image_tasks", "image_refs", "video_inputs",
                      "video_tasks", "publish_log", "node_assets", "node_canvases", "topaz_local"):
            self._store.mark_dirty(table)
        return changed

    def _rewrite_topaz_out_urls(self) -> None:
        """老版 /files/outputs/topaz/ 前缀统一换到 /files/topaz/（对应 rewriteTopazOutUrls）"""
        old = "/files/outputs/topaz/"
        new = "/files/topaz/"

        def fix(holder: Any, field: str) -> None:
            if not isinstance(holder, dict):
                return
            value = holder.get(field)
            if not isinstance(value, str) or not value.startswith(old):
                return
            holder[field] = new + value[len(old) :]

        store = self._store
        for rec in (store.topaz_local or {}).get("results") or []:
            fix(rec, "outUrl")
        for job in store.topaz_jobs:
            fix(job, "outUrl")
        for task in store.tasks:
            for out in task.get("outputs") or []:
                fix(out, "localUrl")
        for table in ("topaz_local", "topaz_jobs", "tasks"):
            self._store.mark_dirty(table)

    @staticmethod
    def _count_files(directory: Path) -> int:
        total = 0
        for entry in directory.iterdir():
            if entry.is_dir():
                total += Paths._count_files(entry)
            else:
                total += 1
        return total

    def _move_dir_contents(self, src: Path, dst: Path) -> int:
        """把 src 里的内容整体挪到 dst（同名不覆盖，目录递归合并）。对应 moveDirContents。"""
        if not src or not dst or not src.exists():
            return 0
        dst.mkdir(parents=True, exist_ok=True)
        moved = 0
        for entry in list(src.iterdir()):
            dest = dst / entry.name
            if dest.exists():
                if entry.is_dir():
                    moved += self._move_dir_contents(entry, dest)
                continue
            try:
                entry.rename(dest)
            except OSError:
                # 跨盘 rename 会失败，退化成复制 + 删除
                if entry.is_dir():
                    shutil.copytree(entry, dest)
                    shutil.rmtree(entry, ignore_errors=True)
                else:
                    shutil.copy2(entry, dest)
                    entry.unlink(missing_ok=True)
            moved += self._count_files(dest) if dest.is_dir() else 1
        try:
            if not any(src.iterdir()):
                src.rmdir()
        except OSError:
            pass
        return moved


def repair_moved_local_paths(paths: Paths) -> int:
    """启动时补救「改过目录但没回写路径」的悬空记录（对应 repairMovedLocalPaths）。

    三条都满足才动：记录仍指向默认目录、文件已不在默认目录、却在新目录里找得到。
    """
    store = paths._store
    total = 0
    for spec in STORAGE_SPECS:
        key = spec["key"]
        if not paths._custom(key):
            continue
        current = paths.dirs.get(key)
        if not current:
            continue
        fallback = paths.default_path_for(key)
        if fallback.resolve() == current.resolve():
            continue
        src, dst = fallback.resolve(), current.resolve()

        available: set[str] | None = None

        def exists(rel: str) -> bool:
            nonlocal available
            if available is None:
                available = set()
                for root, _dirs, files in os.walk(dst):
                    for name in files:
                        full = Path(root) / name
                        try:
                            available.add(str(full.relative_to(dst).as_posix()))
                        except ValueError:
                            continue
            return rel in available

        changed = 0

        def fix_if_moved(holder: Any, field: str) -> None:
            nonlocal changed
            if not isinstance(holder, dict):
                return
            value = holder.get(field)
            if not isinstance(value, str) or not value.strip():
                return
            abs_path = Path(value).resolve()
            try:
                rel = abs_path.relative_to(src)
            except ValueError:
                return
            if not rel.parts:
                return
            # 旧位置还在（文件没搬）或新位置也没有：都不动
            if abs_path.exists() or not exists(rel.as_posix()):
                return
            holder[field] = str(dst / rel)
            changed += 1

        for canvas in store.canvases:
            for asset in (canvas.get("assets") or {}).values():
                fix_if_moved(asset, "localPath")
        for task in store.tasks:
            for out in task.get("outputs") or []:
                fix_if_moved(out, "localPath")
        for task in store.image_tasks:
            for img in task.get("images") or []:
                fix_if_moved(img, "localPath")
            for ref in task.get("refs") or []:
                fix_if_moved(ref, "localPath")
        for ref in store.image_refs:
            fix_if_moved(ref, "localPath")
        for inp in store.video_inputs:
            fix_if_moved(inp, "localPath")
        for task in store.video_tasks:
            fix_if_moved(task.get("input") or {}, "localPath")
            fix_if_moved(task.get("video") or {}, "localPath")
        for rec in store.publish_log:
            fix_if_moved(rec, "path")
        for asset in store.node_assets:
            fix_if_moved(asset, "localPath")
        for graph in store.node_canvases:
            for node in graph.get("nodes") or []:
                fix_if_moved(node, "localPath")

        if changed:
            log.info("[store] 补回 %s 条随「%s」搬到新位置的记录路径", changed, spec["label"])
            total += changed
    return total
