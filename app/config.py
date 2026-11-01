"""全局配置：路径、环境变量、常量。

对应 Node 版 `server/store.js:11-20` 的 ROOT / DATA_DIR 约定，以及
`electron/main.mjs:32` 注入 `RH_DATA_DIR` 的做法 —— 打包后程序目录只读，
数据必须落到用户数据目录。
"""

from __future__ import annotations

import os
from pathlib import Path

# 项目根：本文件在 app/config.py，往上两级
ROOT = Path(__file__).resolve().parent.parent

# 数据目录优先级：环境变量 > 用户数据目录（桌面模式）> 项目内 data/（开发模式）
_env_dir = os.environ.get("LUMEN_DATA_DIR") or os.environ.get("RH_DATA_DIR")
if _env_dir:
    DATA_DIR = Path(_env_dir).resolve()
elif os.environ.get("LUMEN_DESKTOP") == "1":
    DATA_DIR = Path(os.environ.get("APPDATA", ROOT)) / "rh-canvas-studio" / "data"
else:
    DATA_DIR = ROOT / "data"

DATA_DIR.mkdir(parents=True, exist_ok=True)

# SQLite 库文件（替代原 db.json）
DB_FILE = DATA_DIR / "lumen.db"

# 原 db.json：仅用于首次迁移导入
LEGACY_DB_FILE = DATA_DIR / "db.json"

# 前端静态资源
PUBLIC_DIR = ROOT / "public"

# 上传 / 生成产物的落盘子目录（与原版 storage 结构一致）
UPLOADS_DIR = DATA_DIR / "uploads"
OUTPUTS_DIR = DATA_DIR / "outputs"
GENERATED_DIR = DATA_DIR / "generated"

for _d in (UPLOADS_DIR, OUTPUTS_DIR, GENERATED_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# HTTP 服务
HOST = os.environ.get("LUMEN_HOST", "127.0.0.1")
PORT = int(os.environ.get("LUMEN_PORT", "0"))  # 0 = 由系统分配

# 免费额度窗口期：24 小时（对应 scheduler.js:75 QUOTA_WINDOW_MS）
QUOTA_WINDOW_MS = 24 * 60 * 60 * 1000

# 调度器节奏（对应 scheduler.js:8-20）
TICK_MS = 2000
POLL_MS = 4000
TASK_TIMEOUT_MS = 2 * 60 * 60 * 1000
LONG_WAIT_POLL_MS = 60 * 1000
MAX_POLL_ERRORS = 10
QUEUE_FULL_COOLDOWN_MS = 60 * 1000
RESUME_BACKOFF_MS = 60 * 1000

# 余额查询
BALANCE_TTL_MS = 5 * 60 * 1000
BALANCE_STARTUP_DELAY_MS = 2500

# 生图约束（对应 imagegen.js:278/293）
MAX_REFS = 10
MAX_REF_BYTES = 10 * 1024 * 1024
MAX_COUNT = 4

# 任务表保留条数（对应 store.js:505-534 flush 的裁剪规则）
KEEP_TASKS = 400
KEEP_IMAGE_TASKS = 60
KEEP_VIDEO_TASKS = 60
KEEP_STORYBOARDS = 50
KEEP_TOPICS = 300
KEEP_PUBLISH_LOG = 200
MAX_IMAGE_REFS = 120

# 密钥加密前缀（对应 secret.js:15）
SECRET_PREFIX = "enc:v1:"

# 需要加密落库的字段：表名 → 字段名（对应 secret.js:59-63）
SECRET_FIELDS = [
    ("accounts", "apiKey"),
    ("image_models", "apiKey"),
    ("video_models", "apiKey"),
]
