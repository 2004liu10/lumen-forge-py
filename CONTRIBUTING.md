# 贡献指南

## 环境

- Python 3.11+
- Windows 上所有命令**必须带 `-X utf8`**，否则中文日志会触发 `UnicodeEncodeError`

```bash
pip install -e ".[dev]"
python -X utf8 run.py --port 8791 --no-open
```

## 提交前自检

```bash
python -X utf8 tools/smoke_imports.py    # 25/25 ok
python -X utf8 tools/check_routes.py     # total=129 done=129 missing=0
python -X utf8 tools/smoke_api.py        # 通过 43 项
python -X utf8 tools/smoke_materials.py
```

三条全绿再提 PR。CI 会跑同样的四项。

## 几条硬性约定

**1. `/api` 契约不能随便改。**
`public/` 里的前端是从原版原样复制的，它按原版字段名（驼峰）解析响应。
`tools/contract.json` 是 129 条路由的基线，由 `tools/extract_contract.py` 从原版
`server/` 下全部 `.js` 机械提取。加接口可以，改已有接口的路径/字段名会直接打断前端。

**2. `store` 的访问语义要对齐原版。**
`store.accounts` 拿到 list，`store.settings` 拿到 dict。
业务代码基本是照抄原版 JS 的，所以这些用法不能"顺手优化"掉。

**3. 只调 `store.save()` 的地方别改成 `mark_dirty(单表)`。**
原版 `save()` 重写整个 `db.json`，等价于"全部表落盘"。
这里 `save()` 会把已加载的全部表标脏 —— 移植代码里有 8 处只调了 `save()`。
改成单表标脏会静默丢数据。

**4. 新增上游协议时，加在 `app/providers/` 里，不要塞进路由层。**

## 目录职责

| 目录 | 职责 |
|---|---|
| `app/api/` | 只做 HTTP 层：解析入参、调业务模块、拼响应 |
| `app/providers/` | 上游 API 适配（生图 9 种协议、生视频 4 家） |
| `app/*.py` | 业务模块，按资源一个文件 |
| `public/` | 前端，**不要改**，除非同步改原版 |
| `tools/` | 开发期校验脚本，不参与运行时 |

## 已知未完成

- `app/topaz.py` 的 `start_enhance` / `start_batch` / `cancel_enhance` / `restore` 是接口空壳，
  抛 `TopazNotImplemented`。接口形状齐全，前端不会崩，但没有实际跑画质提升。
- 抖音发布是半自动（外部浏览器登录），不是原版的全自动。
