"""逐个 import 所有模块，确认没有缺失依赖。"""
import importlib
import os
import sys
import traceback

os.environ.setdefault("LUMEN_DATA_DIR", os.path.join(os.path.dirname(os.path.dirname(__file__)), "data"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

MODULES = [
    "app.config",
    "app.ids",
    "app.secret",
    "app.db",
    "app.store",
    "app.paths",
    "app.models",
    "app.shared_files",
    "app.providers.imagegen",
    "app.providers.runninghub",
    "app.providers.videogen",
    "app.providers.doubao_bridge",
    "app.scheduler",
    "app.image_jobs",
    "app.video_jobs",
    "app.video_frames",
    "app.materials",
    "app.topics",
    "app.publish_log",
    "app.web_accounts",
    "app.web_providers",
    "app.node_canvas",
    "app.storyboard",
    "app.topaz",
    "app.seed",
]

ok = 0
for name in MODULES:
    try:
        importlib.import_module(name)
        print("OK  ", name)
        ok += 1
    except Exception as err:  # noqa: BLE001
        print("FAIL", name, "->", type(err).__name__, err)
        traceback.print_exc()
print(f"\n{ok}/{len(MODULES)} ok")
