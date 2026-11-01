# Lumen Forge · Python 版

[![CI](https://github.com/2004liu10/lumen-forge-py/actions/workflows/ci.yml/badge.svg)](https://github.com/2004liu10/lumen-forge-py/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue.svg)](pyproject.toml)

原项目 `project-generation`（Electron + Express + 手写前端，约 42k 行 JS）的 Python 重写。
**前端原样复用**，后端换成 FastAPI + SQLite，对外 `/api` 契约与原版逐条一致。

多画布 AI 生成工作台：RunningHub 动作迁移、文生图 / 图生图（9 种上游协议）、
文生视频 / 图生视频（4 家）、无限画布、分镜剧本、素材抓取、多账号并发调度。

## 快速开始

```bash
pip install -e .                 # 或 pip install -e ".[desktop]" 带上浏览器自动化
python -X utf8 run.py            # 启动并自动打开浏览器
python -X utf8 run.py --port 8791 --no-open
```

Windows 上**务必带 `-X utf8`**，否则中文日志会触发 `UnicodeEncodeError`。

## 目录结构

```
lumen-forge-py/
├── run.py                  启动入口（解析参数 → bootstrap → uvicorn）
├── pyproject.toml          依赖与元数据（name=lumen-forge, version=0.8.3）
├── app/                    后端全部源码，约 21k 行
│   ├── main.py             create_app / bootstrap / lifespan
│   ├── config.py           路径与常量（数据目录、超时、保留条数…）
│   ├── db.py               SQLite 连接层（WAL + 18 张列表表 + 7 张单例表）
│   ├── store.py            原版内存字典 `db` 的等价物，模块里照旧 `store.xxx`
│   ├── models.py           记录工厂（字段名保持驼峰，前端不用改）
│   ├── ids.py              `prefix_<base36毫秒><5位随机>`
│   ├── secret.py           API Key 落盘加密（无编解码器时明文，与原版一致）
│   ├── paths.py            8 个存储目录，可搬位置
│   ├── seed.py             内置图片模型 / 视频模型 / 素材源
│   ├── scheduler.py        账号池调度、额度轮转、远端任务导入
│   ├── image_jobs.py       生图队列
│   ├── video_jobs.py       生视频队列
│   ├── materials.py        素材抓取 / 下载 / 线路池
│   ├── node_canvas.py      无限画布（节点 / 连线 / 素材库）
│   ├── storyboard.py       分镜剧本
│   ├── canvas_director.py  画布导演（写剧本 → 拆镜 → 出图）
│   ├── canvas_chat.py      画布对话
│   ├── canvas_params.py    工作流参数解析
│   ├── codex_cli.py        Codex CLI 桥
│   ├── topaz.py            画质提升（`start_enhance` 等为接口空壳）
│   ├── topics.py           话题库
│   ├── publish_log.py      发布记录
│   ├── shared_files.py     跨模块文件去重
│   ├── video_frames.py     首帧 / 尾帧抽帧
│   ├── web_accounts.py     网页平台账号池
│   ├── web_providers.py    8 家网页平台内置配置
│   ├── providers/          上游 API 适配
│   │   ├── imagegen.py     9 种生图协议（ark/openai/zhipu/jimeng/wanxiang/hunyuan/minimax/cloudflare/doubao）
│   │   ├── videogen.py     4 家生视频（zhipu/ark/dashscope/doubao）
│   │   ├── runninghub.py   RunningHub
│   │   └── doubao_bridge.py 本地豆包客户端桥
│   └── api/                路由层，一个文件一组资源
│       ├── deps.py         公共层（HttpError / json_body / raw_body / ok …）
│       ├── state.py        首屏聚合
│       ├── accounts.py     账号
│       ├── canvases.py     画布
│       ├── image_tasks.py  图片模型 + 图片任务
│       ├── video_api.py    视频模型 + 视频任务 + 视频输入
│       ├── materials_api.py 素材
│       ├── node_canvas_api.py 节点画布 + 节点素材（含 500MB 原始二进制上传）
│       ├── story_api.py    分镜
│       ├── script_engine_api.py 剧本引擎
│       ├── canvas_ai.py    导演 + 聊天
│       ├── tasks_api.py    任务通用操作
│       ├── topaz_api.py    画质提升
│       ├── topics_api.py   话题
│       ├── publish_api.py  发布
│       ├── storage_api.py  存储位置
│       └── web.py          网页平台 + 网页账号
├── public/                 前端（**从原版原样复制**，未改一行）
├── data/                   运行时数据（lumen.db 及 8 个素材目录）
└── tools/                  开发期校验脚本
    ├── extract_contract.py 从原版 JS 机械提取路由 → contract.json
    ├── contract.json       129 条路由基线
    ├── check_routes.py     比对已注册路由，输出 done/missing
    ├── smoke_api.py        TestClient 冒烟，43 项
    ├── smoke_imports.py    模块导入自检，25 项
    ├── smoke_materials.py  素材模块单元自检
    ├── _ref_b1.md          原版勘察全文（112 条路由 handler 源码）
    └── _ref_routes.md      129 条路由清单
```

## 数据层

原版把整个 `db.json` 读进内存字典，各模块直接 `db.accounts.push(...)`。
这里换成 SQLite，但**保留了同样的访问语义**：

- `store.accounts` 拿到的是 list，`store.settings` 拿到的是 dict，与原版一致
- `store.replace(name, value)` / `store.save()` / `store.flush()` 都在，业务代码几乎照抄
- 18 张列表表 `(id, payload, created_at, updated_at)`，payload 是整条记录的 JSON
- 7 张单例表 `(key, payload)`，key 恒为 `'main'`
- 空库启动时自动从 `data/db.json` 迁移（`store.migrate_legacy()`），原版的 30+ 一次性迁移布尔标记直接跳过

## 与原版的差异（有意为之）

| 项 | 原版 | 这里 |
|---|---|---|
| 桌面壳 | Electron | 直接用浏览器（`[desktop]` 可选依赖里预留 Playwright） |
| 存储 | `db.json` 整文件读写 | SQLite WAL，单连接 + 锁 |
| 画质提升 | Topaz 全流程 | 接口形状齐全，`start_enhance` 等抛 `TopazNotImplemented` |
| 本地豆包出片 | 驱动客户端 | `doubao_bridge.py` 已移植，需要客户端开着调试端口 |
| 抖音发布 | 全自动 | 半自动 + 外部浏览器登录 |

### `store.save()` 的语义

原版 `save()` 是「250ms 后把**整个 db.json** 重写一遍」—— 效果是**所有表**都会落盘。
所以这里 `save()` 会把当前已加载的**全部表**标脏，而不是只标一张：移植过来的代码里有 8 处
只调 `store.save()` 就改完了（`topics.add_topics`、`node_canvas` 若干处、`materials` 线路池等），
只标一张表的话这些改动会静默丢数据。

「标脏全部」不等于「重写全部」：`flush()` -> `_persist()` 会按每行的 JSON 快照比对，
只 upsert 真正变过的行、只 delete 缓存里已经消失的行。

## 自检

```bash
python -X utf8 tools/check_routes.py     # total=129 done=129 missing=0
python -X utf8 tools/smoke_api.py        # 通过 43 项
python -X utf8 tools/smoke_imports.py    # 25/25 ok
```

`check_routes.py` 的基线是 `tools/contract.json`，由 `extract_contract.py` 从原版
`server/` 下全部 `.js` 机械提取 —— 早期只扫模块化的 `router.get(...)` 会漏掉
`server/node-canvas.js` 里 `attachNodeCanvasRoutes(app)` 自注册的 16 条 `app.post(...)`。
