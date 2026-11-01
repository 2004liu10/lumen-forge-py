"""对比 tools/contract.json（原 Node 后端的 112 条路由）与 app/api/ 里已注册的路由。

用法：python -X utf8 tools/check_routes.py
"""
from __future__ import annotations

import glob
import io
import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

ROUTE_RE = re.compile(r'@router\.(get|post|patch|put|delete)\(\s*(["\'])([^"\']+)\2')
EXTRACT_RE = re.compile(r'@router\.(get|post|patch|put|delete)\(\s*(["\'])([^"\']+)\2')


def norm(path: str) -> str:
    """把 /api/accounts/:id 与 /api/accounts/{account_id} 归一成同一种形状"""
    path = re.sub(r':[A-Za-z_][A-Za-z_0-9]*', '{}', path)
    path = re.sub(r'\{[^}]*\}', '{}', path)
    return path.rstrip('/')


def implemented() -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    for name in sorted(glob.glob(os.path.join(ROOT, 'app', 'api', '*.py'))):
        text = io.open(name, encoding='utf-8').read()
        for method, _q, path in ROUTE_RE.findall(text):
            out.add((method.upper(), norm(path)))
    return out


def main() -> int:
    contract = json.load(io.open(os.path.join(ROOT, 'tools', 'contract.json'), encoding='utf-8'))
    routes = contract['routes']
    done = implemented()
    missing = [r for r in routes if (r['method'], norm(r['path'])) not in done]
    extra = sorted(done - {(r['method'], norm(r['path'])) for r in routes})

    print(f'total={len(routes)} done={len(routes) - len(missing)} missing={len(missing)}')
    if missing:
        print('--- MISSING ---')
        for r in missing:
            print(f"  {r['method']:<6} {r['path']:<52} {r['file']}:{r['start_line']}")
    if extra:
        print('--- EXTRA (不在原版契约里) ---')
        for m, p in extra:
            print(f'  {m:<6} {p}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
