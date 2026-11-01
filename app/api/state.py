"""`GET /api/state` —— 前端首屏要的那一个大对象。

原版 `stateSnapshot()`（`server/index.js:300-371`）一口气把 20 多张表的快照拼起来，
前端拿到之后就不再单独查。字段顺序与字段名必须逐一对齐，否则前端读不到。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request

from ..config import DATA_DIR
from ..image_jobs import image_model_snapshot
from ..materials import IMAGE_RANK_MODES, IMAGE_SEARCH_SOURCES, material_source_snapshot
from ..models import normalize_line_pool
from ..node_canvas import node_asset_snapshot, node_canvas_snapshot
from ..paths import repair_moved_local_paths
from ..providers import imagegen, videogen
from ..providers.runninghub import DEFAULT_BASE
from ..publish_log import list_publish_records
from ..scheduler import account_snapshot, get_paths
from ..storyboard import all_storyboard_snapshots, story_model_snapshot, sync_storyboards
from ..store import store
from ..topaz import topaz_snapshot
from ..topics import topic_list, topic_stats
from ..video_jobs import video_input_snapshot, video_model_snapshot

log = logging.getLogger(__name__)

router = APIRouter()


def _recent(rows: list[dict], limit: int) -> list[dict]:
    """按 createdAt 倒序取前 N 条（原版到处在用 `sort((a,b)=>b.createdAt-a.createdAt).slice(0,N)`）"""
    ordered = sorted(rows, key=lambda r: int(r.get("createdAt") or 0), reverse=True)
    return ordered[:limit]


def state_snapshot() -> dict:
    """拼出 `stateSnapshot()`。字段顺序与 `server/index.js:300-371` 完全一致。"""
    paths = get_paths()
    return {
        "accounts": [account_snapshot(a) for a in store.accounts],
        "canvases": store.canvases,
        "tasks": store.tasks[:120],
        "imageModels": [image_model_snapshot(m) for m in store.image_models],
        "imageTasks": _recent(store.image_tasks, 40),
        "videoModels": [video_model_snapshot(m) for m in store.video_models],
        "videoTasks": _recent(store.video_tasks, 40),
        "videoInputs": _recent(store.video_inputs, 40),
        "materialSources": [material_source_snapshot(s) for s in store.material_sources],
        "materialLinePool": normalize_line_pool(store.material_line_pool),
        "imageSearchSources": IMAGE_SEARCH_SOURCES,
        "imageRankModes": IMAGE_RANK_MODES,
        "defaults": {"baseUrl": DEFAULT_BASE},
        "storage": paths.snapshot(),
        "dataDir": str(DATA_DIR),
        "imageDefaults": {
            "arkBaseUrl": imagegen.ARK_BASE,
            "arkModels": imagegen.ARK_MODEL_PRESETS,
            "sizePresets": imagegen.SIZE_PRESETS,
            "ratioPresets": imagegen.IMAGE_RATIO_PRESETS,
            "maxRefs": imagegen.MAX_REFS,
            "zhipuBaseUrl": imagegen.ZHIPU_BASE,
            "zhipuModels": imagegen.ZHIPU_MODEL_PRESETS,
            "zhipuSizePresets": imagegen.ZHIPU_SIZE_PRESETS,
            "jimengBaseUrl": imagegen.JIMENG_BASE,
            "jimengModels": imagegen.JIMENG_MODEL_PRESETS,
            "jimengSizePresets": imagegen.JIMENG_SIZE_PRESETS,
            "wanxiangBaseUrl": imagegen.WANXIANG_BASE,
            "wanxiangModels": imagegen.WANXIANG_MODEL_PRESETS,
            "wanxiangFunctions": imagegen.WANXIANG_FUNCTIONS,
            "hunyuanBaseUrl": imagegen.HUNYUAN_BASE,
            "hunyuanModels": imagegen.HUNYUAN_MODEL_PRESETS,
            "hunyuanSizePresets": imagegen.HUNYUAN_SIZE_PRESETS,
            "minimaxBaseUrl": imagegen.MINIMAX_BASE,
            "minimaxModels": imagegen.MINIMAX_MODEL_PRESETS,
            "minimaxSizePresets": imagegen.MINIMAX_SIZE_PRESETS,
            "cloudflareBaseUrl": imagegen.CLOUDFLARE_BASE,
            "cloudflareModels": imagegen.CLOUDFLARE_MODEL_PRESETS,
            "cloudflareSizePresets": imagegen.CLOUDFLARE_SIZE_PRESETS,
        },
        "videoDefaults": {"providers": videogen.PROVIDER_PRESETS},
        "storyboards": all_storyboard_snapshots(),
        "storyDefaults": story_model_snapshot(),
        "topics": topic_list(),
        "topicStats": topic_stats(),
        "publishLog": list_publish_records(),
        "topaz": topaz_snapshot(),
        "nodeCanvases": node_canvas_snapshot(),
        "nodeAssets": node_asset_snapshot(),
    }


@router.get("/api/state")
async def get_state(request: Request) -> dict:
    # 先把剧本里画好的图同步回来（同步失败不能拖垮整个首屏）
    try:
        sync_storyboards()
    except Exception as err:  # noqa: BLE001 - 与原版一致：只记日志
        log.error("[state] 同步剧本失败：%s", err)
    try:
        repair_moved_local_paths(get_paths())
    except Exception as err:  # noqa: BLE001
        log.warning("[state] 修复素材路径失败：%s", err)
    return state_snapshot()


__all__ = ["router", "state_snapshot"]
