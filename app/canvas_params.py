"""画布参数辅助 —— 复刻 `server/index.js` 里的 `parseWorkflowJson` / `pruneValues` / `fieldLabel`。

这三段原本嵌在 `POST /api/canvases/:id/inspect` 的处理函数里，与 RunningHub 的
「应用参数 / 工作流 JSON」两套输入格式强相关。抽出来单独放，路由层就能保持薄。
"""

from __future__ import annotations

import json
from typing import Any

from .scheduler import field_key, get_paths, rh_field_is_file
from .shared_files import unlink_if_unused
from .store import store


def field_label(*, field: dict, value: Any) -> str:
    """`fieldLabel({field, value})` —— 自动关水印时给用户看的「改了哪一项」"""
    desc = field.get("description") or field.get("nodeName") or field.get("fieldName") or ""
    return f"{desc}={value}"


def parse_workflow_json(raw: str) -> list[dict]:
    """把导出的 `api.json`（ComfyUI 工作流）拆成可填参数列表。

    形状兼容两种：`{prompt: {...}}`（新导出）和 `{...}`（老导出，直接就是节点表）。
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as err:
        raise ValueError(f"JSON 解析失败：{err}") from err

    nodes = data.get("prompt") if isinstance(data, dict) and data.get("prompt") is not None else data
    if not isinstance(nodes, dict):
        raise ValueError("未能从该 JSON 中解析出可填参数")

    fields: list[dict] = []
    for node_id, node in nodes.items():
        if not isinstance(node, dict):
            continue
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            continue
        meta = node.get("_meta") if isinstance(node.get("_meta"), dict) else {}
        title = meta.get("title") or node.get("class_type") or ""
        class_type = node.get("class_type") or ""
        for field_name, value in inputs.items():
            if isinstance(value, (list, type(None))) or isinstance(value, (dict, bool, int, float)):
                # 数组 / null / 对象 都不是「用户能填的标量」；bool 是标量，下面单独判
                if not isinstance(value, bool):
                    continue
            lowered = str(field_name).lower()
            if "video" in lowered:
                field_type = "VIDEO"
            elif "audio" in lowered:
                field_type = "AUDIO"
            elif "image" in lowered or "mask" in lowered:
                field_type = "IMAGE"
            elif isinstance(value, bool):
                field_type = "LIST"
            elif isinstance(value, (int, float)):
                field_type = "NUMBER"
            else:
                field_type = "STRING"
            options: list[dict] = []
            if field_type == "LIST":
                options = [{"index": "true", "label": "true"}, {"index": "false", "label": "false"}]
            fields.append(
                {
                    "nodeId": str(node_id),
                    "nodeName": class_type,
                    "fieldName": str(field_name),
                    "fieldType": field_type,
                    "description": f"{title} · {field_name}" if title else str(field_name),
                    "defaultValue": str(value),
                    "options": options,
                }
            )
    return fields


def prune_values(canvas: dict) -> None:
    """把 `values` / `assets` 里已经不在 `fields` 中的项清掉，并回收不再被引用的文件。

    改过 appId 之后旧的参数值必须删：留着会被当成新参数提交，或者让界面显示一堆
    「已失效」的输入框。
    """
    fields = canvas.get("fields") or []
    valid = {field_key(f) for f in fields}
    values = canvas.get("values") if isinstance(canvas.get("values"), dict) else {}
    for key in list(values.keys()):
        if key not in valid:
            values.pop(key, None)
            continue
        # LIST 字段还要求值仍在候选里：换过工作流之后老的选项值会变得不可提交
        field = next((f for f in fields if field_key(f) == key), None)
        options = (field or {}).get("options") or []
        if (field or {}).get("fieldType") == "LIST" and options:
            allowed = {str(o.get("index")) for o in options}
            if str(values.get(key)) not in allowed:
                values.pop(key, None)
    canvas["values"] = values

    assets = canvas.get("assets") if isinstance(canvas.get("assets"), dict) else {}
    dropped: list[str] = []
    for key, asset in list(assets.items()):
        if key not in valid:
            dropped.append(str((asset or {}).get("localPath") or ""))
            assets.pop(key, None)
    canvas["assets"] = assets
    for path in dropped:
        if path:
            unlink_if_unused(path)


def files_url_prefix(abs_path: str, *, paths=None) -> str:
    """按文件实际所在的媒体目录给出 `/files/...` 前缀（改过存储位置也能正确访问）"""
    from .node_canvas import files_url_for

    if paths is None:
        paths = get_paths()
    url = files_url_for(abs_path, paths)
    return url


def new_canvas_asset(*, field: dict, local_path: str, original_name: str, mime_type: str, size: int, origin: str, local_url: str) -> dict:
    """构造 `canvas.assets[key]` 里的那一份记录（画布素材 / 生成结果送入都走这里）"""
    from .shared_files import share_file

    shared = share_file(local_path=local_path, original_name=original_name, mime_type=mime_type, size=size, origin=origin)
    return {**shared, "localUrl": local_url, "uploadedAt": store.now()}


__all__ = [
    "field_label",
    "parse_workflow_json",
    "prune_values",
    "files_url_prefix",
    "new_canvas_asset",
    "rh_field_is_file",
]
