"""共享文件：引用而不复制，没人用了才真删。

## 要解决的问题

原版曾经把「加进画布」「送去视频」「加参考图」都做成**物理复制一份**，每次还生成
新的 `nextId` 文件名 —— 同一个视频加 3 个画布就是 3 份物理副本。素材越多磁盘涨得越快，
删除逻辑散在 20 多处，谁都不敢动。

现在改成：**文件只在它的「源目录」存一份，其它地方只记路径**（记 `origin` 便于追溯）。

代价是「删一侧会不会把另一侧的文件删掉」这个风险必须正面解决 ——
所以所有删除都走 `unlink_if_unused()`：先扫全库数引用，归零才真删。

## 为什么用「扫全库」而不是维护计数表

计数表要在每个增删点同步加减，漏一处就永久偏差（文件永远删不掉，或误删）。
全库扫描虽然每次 O(n)，但库里最多几千条记录，且删除是低频操作，够快也不会错。
"""

from __future__ import annotations

import logging
from pathlib import Path

from .store import store

log = logging.getLogger(__name__)


def _abs(value) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return str(Path(text).resolve())
    except OSError:
        return text


def collect_references(*, include_publish_log: bool = True) -> dict[str, int]:
    """收集全库所有「正在被引用的本地文件路径」——**按记录计数**，不是去重的 set。

    为什么不去重：同一个文件被 3 个地方共享时，我们要知道「有 3 条记录在用它」，
    才能正确判断「摘掉我这条之后还剩几条」。

    `include_publish_log`：发布记录算不算「在用」，默认算。
    理由：发布记录指向的是**用户的成品**。如果删个画布素材就把它顺手清掉了，
    用户根本想不到 —— 发布过的视频要保持存在，直到他主动去发布页点「删除已发布视频」。
    只有那个功能自己分析清单时才传 False，否则「发布记录引用着自己」，
    所有文件都会被判成有人用、永远删不掉。
    """
    counts: dict[str, int] = {}

    def hit(path_value) -> None:
        abs_path = _abs(path_value)
        if not abs_path:
            return
        counts[abs_path] = counts.get(abs_path, 0) + 1

    # 凡是会指向本地文件的字段都必须列全 —— 漏一个，文件就可能被误删
    for canvas in store.canvases:
        for asset in (canvas.get("assets") or {}).values():
            hit((asset or {}).get("localPath"))
    for task in store.tasks:
        for out in task.get("outputs") or []:
            hit((out or {}).get("localPath"))
    for task in store.image_tasks:
        for img in task.get("images") or []:
            hit((img or {}).get("localPath"))
        for ref in task.get("refs") or []:
            hit((ref or {}).get("localPath"))
    for ref in store.image_refs:
        hit(ref.get("localPath"))
    for input_ in store.video_inputs:
        hit(input_.get("localPath"))
    for task in store.video_tasks:
        hit((task.get("input") or {}).get("localPath"))
        hit((task.get("video") or {}).get("localPath"))
        hit((task.get("endInput") or {}).get("localPath"))
    # 分镜角色的「定妆照」：角色上存的是 refId（参考图库里的 id），不是路径，
    # 所以要按 id 找到那份参考图再计数。原版这里曾写成 `c.refLocalPath` —— 这个字段
    # 全库根本不存在，等于那一行从未生效，定妆照被当成「没人用」而删掉。
    for board in store.storyboards:
        for character in board.get("characters") or []:
            ref_id = (character or {}).get("refId")
            if not ref_id:
                continue
            ref = store.find("image_refs", id=ref_id)
            if ref:
                hit(ref.get("localPath"))
    # 节点画布：素材库上传的文件被拖进画布后，节点上记的是同一份 localPath
    for asset in store.node_assets:
        hit(asset.get("localPath"))
    for graph in store.node_canvases:
        for node in graph.get("nodes") or []:
            hit((node or {}).get("localPath"))
    # 发布过的视频是成品：默认算「在用」，不许被顺手清掉
    if include_publish_log:
        for record in store.publish_log:
            hit(record.get("path"))
    return counts


def count_references(target, *, include_publish_log: bool = True) -> int:
    """某个路径被多少条记录引用"""
    want = _abs(target)
    if not want:
        return 0
    return collect_references(include_publish_log=include_publish_log).get(want, 0)


def is_path_in_use(target, *, exclude_count: int = 0, include_publish_log: bool = True) -> bool:
    """这个文件还有没有别人在用。

    `exclude_count`：调用方自己占了几条引用 —— 调用方通常「已经从库里摘掉自己那条」
    或「正准备摘」，传 1 表示「把我自己那条扣掉再算」。默认 0 = 直接看当前引用数。
    """
    return count_references(target, include_publish_log=include_publish_log) - int(exclude_count or 0) > 0


def unlink_if_unused(target, *, exclude_count: int = 0, include_publish_log: bool = True) -> bool:
    """安全删除：确认「扣掉调用方自己那条之后」没有任何记录再引用它，才真删。

    返回 True = 删了；False = 还有人用（或文件本来就不在），跳过。
    调用方务必先把自己那条记录从库里摘掉（或传 `exclude_count=1`），
    否则自己那条引用会让文件永远删不掉。
    """
    raw = str(target or "").strip()
    if not raw:
        return False
    abs_path = _abs(raw)
    path = Path(abs_path)
    try:
        if not path.exists():
            return False
    except OSError:
        return False
    if count_references(abs_path, include_publish_log=include_publish_log) - int(exclude_count or 0) > 0:
        return False
    try:
        path.unlink()
        return True
    except OSError as err:
        # 删不掉（文件被占用等）不致命，下次还有机会
        log.debug("[shared-files] 删除失败 %s：%s", abs_path, err)
        return False


def share_file(
    *, local_path, original_name: str | None = None, mime_type: str | None = None, size: int | None = None, origin: str = ""
) -> dict:
    """把「已有的一份文件」登记成另一个地方要用的素材 —— **不复制**，只记路径。

    返回一个可以直接塞进 `canvas.assets` / `videoInputs` 等位置的字段对象。
    `origin` 用来标记「这份文件是谁的源」，排查问题时能看出共享关系。

    `localUrl` 由调用方按自己的 `/files/<前缀>/` 规则拼 —— 同一个文件在不同模块
    看到的 URL 前缀不同（uploads / materials / video-inputs），所以这里不写死。
    """
    abs_path = _abs(local_path)
    final_size = int(size or 0)
    if not final_size:
        try:
            final_size = Path(abs_path).stat().st_size
        except OSError:
            final_size = 0
    return {
        "localPath": abs_path,
        "originalName": original_name or Path(abs_path).name,
        "mimeType": mime_type or "",
        "size": final_size,
        "shared": True,  # 标记：不是这个目录下的独立副本，别按「删自己目录」来清
        "origin": str(origin or ""),
    }
