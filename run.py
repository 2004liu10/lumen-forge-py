"""启动脚本 —— 对应原版 `node server/index.js` 的浏览器版入口。

用法：
    python run.py                # 起在随机空闲端口，自动开浏览器
    python run.py --port 8787    # 指定端口
    python run.py --no-open      # 不自动开浏览器
    python run.py --host 0.0.0.0 # 局域网可访问（默认只听 127.0.0.1）

为什么默认端口是 0：原版桌面版也是让系统分配空闲端口（`port: PORT` 传 0 时），
这样同时开两个实例不会因为端口被占而启动失败。
"""

from __future__ import annotations

import argparse
import logging
import os
import socket
import sys
import threading
import webbrowser
from pathlib import Path


def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        # Windows 控制台默认 GBK，中文日志会炸；强制 UTF-8
        stream=sys.stdout,
    )
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - 某些终端不支持
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="启动 Lumen Forge（浏览器版）")
    parser.add_argument("--port", type=int, default=int(os.environ.get("LUMEN_PORT", "0")),
                        help="监听端口，0 = 由系统分配空闲端口（默认）")
    parser.add_argument("--host", default=os.environ.get("LUMEN_HOST", "127.0.0.1"),
                        help="监听地址，默认 127.0.0.1（只本机）")
    parser.add_argument("--no-open", action="store_true", help="不要自动打开浏览器")
    parser.add_argument("--verbose", "-v", action="store_true", help="输出调试日志")
    args = parser.parse_args(argv)

    _configure_logging(args.verbose)

    # 数据准备必须在 import app.main 之前完成一部分（Paths 注入）。
    # 这里直接走 bootstrap，再拿 create_app 出来的实例。
    from app.config import DATA_DIR
    from app.main import bootstrap, create_app

    bootstrap()
    app = create_app()

    import uvicorn

    # uvicorn 在 port=0 时会把真实端口写回 config.port 吗？不会 ——
    # 得先自己抢一个空闲端口，再交给它，否则拿不到真实地址。
    port = args.port
    if port == 0:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind((args.host, 0))
            port = probe.getsockname()[1]

    app.state.port = port
    url = f"http://{'127.0.0.1' if args.host == '0.0.0.0' else args.host}:{port}"

    print("")
    print("  Lumen Forge 已启动（浏览器版）")
    print(f"  界面地址：{url}")
    print(f"  数据目录：{DATA_DIR}")
    print("")

    if not args.no_open:
        # 等 uvicorn 真的绑好端口再开浏览器，否则会先看到「无法连接」
        def _open_later() -> None:
            import time

            time.sleep(1.2)
            try:
                webbrowser.open(url)
            except Exception:  # noqa: BLE001
                pass

        threading.Thread(target=_open_later, daemon=True).start()

    config = uvicorn.Config(
        app,
        host=args.host,
        port=port,
        log_level="debug" if args.verbose else "info",
        access_log=args.verbose,
    )
    uvicorn.Server(config).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
