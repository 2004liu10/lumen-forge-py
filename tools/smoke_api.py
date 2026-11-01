"""冒烟测试 —— 用 TestClient 真跑一遍所有路由，不连外网、不改真实库。

用法：`python -X utf8 tools/smoke_api.py`
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

# 用一个临时数据目录，绝不能污染 data/
_TMP = Path(tempfile.mkdtemp(prefix="lumen-smoke-"))
os.environ["LUMEN_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import bootstrap, create_app  # noqa: E402

bootstrap()
app = create_app()
client = TestClient(app, raise_server_exceptions=False)

FAIL: list[str] = []
OK: list[str] = []


def check(label: str, response, *, expect=200, keys: tuple[str, ...] = ()) -> dict | list | None:
    """`expect` 可以是 int 或 tuple[int, ...]（多个可接受状态码）。"""
    wanted = expect if isinstance(expect, tuple) else (expect,)
    body = None
    try:
        body = response.json()
    except Exception:  # noqa: BLE001 - 有些路由回空体
        body = None
    if response.status_code not in wanted:
        FAIL.append(f"{label}: HTTP {response.status_code}（期望 {wanted}）→ {response.text[:200]}")
        return body
    if keys and isinstance(body, dict):
        missing = [k for k in keys if k not in body]
        if missing:
            FAIL.append(f"{label}: 缺字段 {missing}")
            return body
    OK.append(f"{label} [{response.status_code}]")
    return body


with client:
    # ---------------------------------------------------------------- 首屏
    state = check("GET /api/state", client.get("/api/state"))
    if isinstance(state, dict):
        need = [
            "accounts", "canvases", "tasks", "imageModels", "imageTasks", "videoModels",
            "videoTasks", "videoInputs", "materialSources", "materialLinePool",
            "imageSearchSources", "imageRankModes", "defaults", "storage", "dataDir",
            "imageDefaults", "videoDefaults", "storyboards", "storyDefaults", "topics",
            "topicStats", "publishLog", "topaz", "nodeCanvases", "nodeAssets",
        ]
        miss = [k for k in need if k not in state]
        if miss:
            FAIL.append(f"GET /api/state 缺字段：{miss}")
        else:
            OK.append(f"GET /api/state 字段齐全（{len(state)} 个顶层键）")

    # ---------------------------------------------------------------- 账号
    acc = check(
        "POST /api/accounts",
        client.post("/api/accounts", json={"name": "冒烟账号", "apiKey": "sk-smoke"}),
        keys=("id", "name"),
    )
    account_id = (acc or {}).get("id") if isinstance(acc, dict) else None
    if account_id:
        check(f"PATCH /api/accounts/{account_id}", client.patch(f"/api/accounts/{account_id}", json={"remark": "x"}))
        check(f"GET /api/accounts/{account_id}/credentials", client.get(f"/api/accounts/{account_id}/credentials"))

    # ---------------------------------------------------------------- 画布
    canvases = state.get("canvases") if isinstance(state, dict) else None
    canvas_id = canvases[0]["id"] if canvases else None
    if canvas_id:
        check(f"POST /api/canvases/{canvas_id}/inspect（空体缺 webappId 应 400）", client.post(f"/api/canvases/{canvas_id}/inspect", json={}), expect=400)
        check(f"POST /api/canvases/{canvas_id}/extra-params", client.post(f"/api/canvases/{canvas_id}/extra-params", json={"extraParams": "[]"}))

    # ---------------------------------------------------------------- 任务
    check("GET /api/tasks", client.get("/api/tasks"))
    check("POST /api/tasks/fetch-missing", client.post("/api/tasks/fetch-missing"))
    check("POST /api/tasks/import（缺参应 400）", client.post("/api/tasks/import", json={}), expect=400)

    # ---------------------------------------------------------------- 生图
    check("POST /api/image-models", client.post("/api/image-models", json={"name": "冒烟模型", "protocol": "ark", "model": "x"}), keys=("id",))
    check("POST /api/image-tasks", client.post("/api/image-tasks", json={"prompt": "一只猫"}), expect=400)

    # ---------------------------------------------------------------- 视频
    check("POST /api/video-models", client.post("/api/video-models", json={"name": "冒烟视频", "protocol": "zhipu"}), expect=200)

    # ---------------------------------------------------------------- 分镜
    check("GET /api/story-model", client.get("/api/story-model"))
    check("GET /api/storyboards", client.get("/api/storyboards"))
    check("GET /api/script-engine", client.get("/api/script-engine"))

    # ---------------------------------------------------------------- 编导 / 聊天
    check("GET /api/canvas/director", client.get("/api/canvas/director"), keys=("styles",))
    check("POST /api/canvas/director（空剧本应 400）", client.post("/api/canvas/director", json={}), expect=400)
    check("POST /api/canvas/chat（空消息应 400）", client.post("/api/canvas/chat", json={}), expect=400)

    # ---------------------------------------------------------------- Topaz
    check("GET /api/topaz/candidates", client.get("/api/topaz/candidates"))
    check("POST /api/topaz/clear-failed", client.post("/api/topaz/clear-failed"))
    check("POST /api/topaz/look", client.post("/api/topaz/look", json={"look": "natural"}))
    check("POST /api/topaz/local（无 dir = 重扫）", client.post("/api/topaz/local", json={}))
    check("GET /api/topaz/local-file（不存在应 404）", client.get("/api/topaz/local-file?path=C:/nope.mp4"), expect=404)

    # ---------------------------------------------------------------- 存储
    check("POST /api/storage（未知 key 应 400）", client.post("/api/storage", json={"key": "nope"}), expect=400)

    # ---------------------------------------------------------------- 素材 / 话题 / 发布
    check("GET /api/materials/random（无外网应 400）", client.get("/api/materials/random"), expect=(200, 400))
    check("GET /api/materials/search（无外网应 400）", client.get("/api/materials/search?q=猫"), expect=(200, 400))
    check("GET /api/topics", client.get("/api/topics"))
    check("GET /api/publish/records", client.get("/api/publish/records"))
    check("GET /api/publish/works", client.get("/api/publish/works"))

    # ---------------------------------------------------------------- 网页平台
    check("GET /api/web-providers", client.get("/api/web-providers"))
    check("GET /api/web-accounts", client.get("/api/web-accounts"))

    # ---------------------------------------------------------------- 节点画布
    # 节点画布没有列表路由 —— 列表在 GET /api/state 的 nodeCanvases 里
    created = check("POST /api/node-canvases", client.post("/api/node-canvases", json={"name": "冒烟画布"}), keys=("id",))
    if isinstance(created, dict) and created.get("id"):
        nid = created["id"]
        check(f"PATCH /api/node-canvases/{nid}", client.patch(f"/api/node-canvases/{nid}", json={"nodes": [], "edges": []}))
        check(f"GET /api/node-canvases/{nid}/export", client.get(f"/api/node-canvases/{nid}/export"))
        check(f"POST /api/node-canvases/{nid}/chat（新对话）", client.post(f"/api/node-canvases/{nid}/chat", json={}))
        check(f"DELETE /api/node-canvases/{nid}", client.delete(f"/api/node-canvases/{nid}"))

    assets = check("GET /api/node-assets", client.get("/api/node-assets"), keys=("items", "trash"))
    check("POST /api/node-assets（空体应 400）", client.post("/api/node-assets", content=b""), expect=400)
    check("POST /api/node-assets/register（不存在的文件应 400）", client.post("/api/node-assets/register", json={"localPath": "C:/nope.png"}), expect=400)
    check("POST /api/node-assets/clear-trash", client.post("/api/node-assets/clear-trash"))

    # ---------------------------------------------------------------- 静态
    check("GET /files/uploads/（不存在应 404）", client.get("/files/uploads/nope.png"), expect=404)
    check("GET /files/../穿越应 404", client.get("/files/..%2F..%2Fsecret.txt"), expect=404)

print("")
print(f"通过 {len(OK)} 项")
if FAIL:
    print(f"失败 {len(FAIL)} 项：")
    for line in FAIL:
        print("  ✗", line)
    raise SystemExit(1)
print("全部通过")
