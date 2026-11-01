"""重新生成 tools/contract.json —— 从原版 JS 源码里机械提取全部路由。

早期版本只扫了 `server/index.js` 等「模块化」文件，漏掉了
`server/node-canvas.js:365` 的 `attachNodeCanvasRoutes(app)` ——
它是自己往 app 上挂的 `app.post(...)` 风格，所以不在 112 条基线里。

这里统一扫 `app.<method>(...)` 与 `router.<method>(...)` 两种写法，
得到的全量是 **129 条**。用法：`python -X utf8 tools/extract_contract.py`

契约条目形状（与旧版兼容）：
    {"method": "GET", "path": "/api/accounts",
     "file": "server/index.js", "start_line": 1, "end_line": 1,
     "loc": 0, "body": ""}

`body` / `loc` 从原版带过来，但 129 条的 handler 源码会让文件膨胀到几 MB；
本版只保留 `method/path/file/start_line`，够 `check_routes.py` 比对就行。
"""
from __future__ import annotations

import io
import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(os.path.dirname(ROOT), 'project-generation', 'server')
OUT = os.path.join(ROOT, 'tools', 'contract.json')

#: `app.get('/api/x', ...)` / `router.post('/api/x/:id', ...)`
ROUTE_RE = re.compile(
    r"""\b(?:app|router)\s*\.\s*(get|post|put|patch|delete)\s*\(\s*(['"])([^'"]+)\2""",
    re.IGNORECASE,
)

#: 这些文件不属于后端（构建脚本、测试）
SKIP = {'node_modules'}


def walk(root: str) -> list[str]:
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP]
        for name in sorted(filenames):
            if name.endswith('.js'):
                out.append(os.path.join(dirpath, name))
    return out


def extract() -> list[dict]:
    routes: list[dict] = []
    for path in walk(SRC):
        text = io.open(path, encoding='utf-8', errors='replace').read()
        rel = os.path.relpath(path, os.path.dirname(SRC)).replace(os.sep, '/')
        for match in ROUTE_RE.finditer(text):
            method, _q, route = match.groups()
            line = text.count('\n', 0, match.start()) + 1
            routes.append(
                {
                    'method': method.upper(),
                    'path': route,
                    'file': rel,
                    'start_line': line,
                    'end_line': line,
                    'loc': 0,
                    'body': '',
                }
            )
    return routes


def dedupe(routes: list[dict]) -> tuple[list[dict], int]:
    """同一 method+path 只留一条（Express 里后注册的会覆盖先注册的，取最后一条）。"""
    seen: dict[tuple[str, str], int] = {}
    out: list[dict] = []
    dups = 0
    for route in routes:
        key = (route['method'], route['path'])
        if key in seen:
            dups += 1
            out[seen[key]] = route
        else:
            seen[key] = len(out)
            out.append(route)
    return out, dups


def main() -> int:
    if not os.path.isdir(SRC):
        print(f'找不到原版源码：{SRC}')
        return 1
    raw = extract()
    routes, dups = dedupe(raw)
    routes.sort(key=lambda r: (r['path'], r['method']))
    payload = {
        'source': os.path.dirname(SRC),
        'total': len(routes),
        'duplicates': dups,
        'raw_total': len(raw),
        'routes': routes,
    }
    io.open(OUT, 'w', encoding='utf-8', newline='\n').write(
        json.dumps(payload, ensure_ascii=False, indent=2) + '\n'
    )
    print(f'raw={len(raw)} unique={len(routes)} duplicates={dups} -> {OUT}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
