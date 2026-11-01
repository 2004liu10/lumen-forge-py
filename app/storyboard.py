"""分镜（小剧场）后端 —— 逐函数复刻 `server/storyboard.js`（21,382 字节 / 652 行）。

原版一条完整链路是：

1. `write_script()`   给一个题材，让文本模型编出「角色设定 + N 个镜头」
2. `cast_character()` 为每个角色单独生成一张「定妆照」
3. `draw_shots()`     每个镜头 = 定妆照(图生图参考图) + 该镜头画面描述 → 一条图片任务
4. （下一期）出视频：把镜头图送去视频模块

**为什么角色要单独出「定妆照」**：图生图如果只靠文字描述锁人物，每张脸都会不一样，
连不成一部剧。先定一张脸、之后每个镜头都拿它当参考图，跨镜头才长得像同一个人。

写剧本只做一种协议：OpenAI 兼容的 `POST {baseUrl}/chat/completions`（主流几家都是这个形状），
配置存在 `store.story_model`（原「抖音标签」模块已删，它的配置由
`migrate_tag_model_to_story` 搬过来）。
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import httpx

from .ids import now_ms
from .image_jobs import (
    create_image_task,
    create_ref_from_image,
    find_image_model,
    find_image_ref,
    find_image_task,
)
from .models import new_storyboard
from .providers.imagegen import REF_MIME_TYPES
from .scheduler import mask_key
from .store import store

#: 写剧本比识图慢（要生成结构化长文本），给足时间。
#: 分镜 prompt 里要求 scene 60~100 字 + setting + endState，一次 5 镜的 JSON 要写上千字，
#: 免费模型（glm-4v-flash）经常写到 2 分钟以上，120 秒会半路掐断。
SCRIPT_TIMEOUT_MS = 240000

#: 一次最多出多少个镜头：再多会让整条链路（串行）跑太久，也容易超模型上下文
MAX_SHOTS = 12
MIN_SHOTS = 3
#: 最多几个角色：定妆照也是一张一张串行出的，角色多了等待时间成倍涨
MAX_CHARACTERS = 4

#: 内置画风候选；界面上也可以自己填
STORY_STYLES = [
    {"id": "cos", "label": "真人 cos（写实摄影）", "prompt": "真人 cos 风格，写实摄影质感，自然光，皮肤质感真实，浅景深"},
    {"id": "cos-studio", "label": "真人 cos（棚拍）", "prompt": "真人 cos 棚拍风格，影棚布光，干净背景，高清商业摄影质感"},
    {"id": "cos-film", "label": "真人 cos（电影感）", "prompt": "真人 cos 电影感风格，胶片颗粒，戏剧性布光，电影级构图"},
]

DEFAULT_STYLE = STORY_STYLES[0]["prompt"]

BASE_URL_RE = re.compile(r"^https?://\S+$", re.I)


# ============================================================ 模型配置


def story_model_snapshot() -> dict:
    """下发给界面的分镜配置：Key 只给掩码"""
    own = store.story_model or {}
    return {
        "baseUrl": str(own.get("baseUrl") or ""),
        "model": str(own.get("model") or ""),
        "apiKey": mask_key(own.get("apiKey")),
        "hasKey": bool(own.get("apiKey")),
        # 编导引擎：'api' = 上面这套 OpenAI 兼容接口；'doubao' = 本地豆包客户端（白嫖）
        "engine": "doubao" if str(own.get("engine") or "") == "doubao" else "api",
        # 写剧本这一层：'' = 不写（用户给什么拆什么）；'codex' = 本地 GPT 先写一版剧本。
        # 跟上面 engine 是两层：engine 决定「谁来拆分镜」，scriptEngine 决定「谁先写剧本」。
        "scriptEngine": "codex" if str(own.get("scriptEngine") or "") == "codex" else "",
        "styles": STORY_STYLES,
        "defaultStyle": DEFAULT_STYLE,
        "maxShots": MAX_SHOTS,
        "minShots": MIN_SHOTS,
        "maxCharacters": MAX_CHARACTERS,
    }


def save_story_model(patch: dict | None = None) -> dict:
    """保存分镜自己的模型配置；apiKey 传空 = 保持原样（回显的是掩码，没重填就不该冲掉）"""
    patch = patch or {}
    cur = store.story_model or {}
    next_cfg = {
        "baseUrl": cur.get("baseUrl") or "",
        "model": cur.get("model") or "",
        "apiKey": cur.get("apiKey") or "",
        "engine": "doubao" if cur.get("engine") == "doubao" else "api",
        "scriptEngine": "codex" if str(cur.get("scriptEngine") or "") == "codex" else "",
    }
    if "baseUrl" in patch:
        base_url = str(patch.get("baseUrl") or "").strip()
        if base_url and not BASE_URL_RE.match(base_url):
            raise ValueError("接口地址要以 http:// 或 https:// 开头")
        next_cfg["baseUrl"] = re.sub(r"/+$", "", base_url)
    if "model" in patch:
        next_cfg["model"] = str(patch.get("model") or "").strip()
    if isinstance(patch.get("apiKey"), str) and patch["apiKey"].strip():
        next_cfg["apiKey"] = patch["apiKey"].strip()
    # 引擎开关：切到豆包时不用校验接口地址和 Key（它压根不调那套接口）
    if "engine" in patch:
        next_cfg["engine"] = "doubao" if str(patch.get("engine") or "").strip() == "doubao" else "api"
    # 写剧本的引擎：切到本地 GPT 时同样不用接口地址和 Key（走 codex.exe）
    if "scriptEngine" in patch:
        next_cfg["scriptEngine"] = "codex" if str(patch.get("scriptEngine") or "").strip() == "codex" else ""
    store.replace("story_model", next_cfg)
    store.save()
    store.flush()
    return story_model_snapshot()


def migrate_tag_model_to_story() -> bool:
    """一次性迁移：把老的 `store.tag_model` 搬进 `store.story_model`。

    背景：抖音标签模块删掉之后，原来「分镜没配就复用标签那套」的回落没了。
    用户当初填在标签里的 Key 是花钱配的，不能让他白填一次 —— 只要 storyModel
    还是空的，就把 tagModel 原样搬过来（有值的不覆盖，避免盖掉用户新填的）。

    返回是否发生了迁移。
    """
    legacy = store.tag_model
    if not isinstance(legacy, dict):
        return False
    if not (legacy.get("baseUrl") or legacy.get("model") or legacy.get("apiKey")):
        return False

    cur = store.story_model or {}
    next_cfg = {
        "baseUrl": re.sub(r"/+$", "", str(cur.get("baseUrl") or legacy.get("baseUrl") or "").strip()),
        "model": str(cur.get("model") or legacy.get("model") or "").strip(),
        "apiKey": str(cur.get("apiKey") or legacy.get("apiKey") or "").strip(),
    }
    # 完全一样就没必要动
    same = (
        str(cur.get("baseUrl") or "") == next_cfg["baseUrl"]
        and str(cur.get("model") or "") == next_cfg["model"]
        and str(cur.get("apiKey") or "") == next_cfg["apiKey"]
    )
    if same:
        return False

    store.replace("story_model", next_cfg)
    store.save()
    store.flush()
    return True


def script_model_config() -> dict:
    """剧本模型配置：用分镜自己配的（不再回落到已删除的 tagModel）"""
    own = store.story_model or {}
    return {
        "baseUrl": re.sub(r"/+$", "", str(own.get("baseUrl") or "").strip()),
        "apiKey": str(own.get("apiKey") or "").strip(),
        "model": str(own.get("model") or "").strip(),
    }


# ============================================================ 写剧本


def _build_script_prompt(
    *,
    idea: str | None,
    shot_count: int,
    style: str,
    character_count: int,
    has_photos: bool,
) -> str:
    """让模型按固定结构出剧本的那段说明"""
    head = (
        "你是一个短视频分镜编剧。用户已经给了人物参考图，请**照着图里的人**编一个适合做成短视频的小剧场分镜。"
        if has_photos
        else "你是一个短视频分镜编剧。请根据用户给的题材，编一个适合做成短视频的小剧场分镜。"
    )
    # 有参考图时 desc 必须「照着图写」：之后每个镜头都拿这张图当参考图，
    # 描述和真人对不上会让模型不知道该画成谁。
    if has_photos:
        char_rule = "\n".join(
            [
                f"- characters：正好 {character_count} 个角色，**按用户给的图片顺序一一对应**（第 1 张图 = 第 1 个角色）。",
                "  · name：给这个角色起个名字。",
                "  · desc：**仔细观察图里的人**，写清楚「性别、年龄段、发型发色、五官特征、服装、气质」，40~70 字。",
                "    这段描述会作为该角色的固定形象说明，之后每个镜头都会带上它 —— 所以必须**如实描述图里看到的样子**，",
                "    不要自己另编一个长相，也不要用「很漂亮」「有气质」这种没法画的话。",
            ]
        )
    else:
        char_rule = "\n".join(
            [
                f"- characters：{character_count} 个角色。desc 必须写清楚「性别、年龄段、发型发色、五官特征、服装、气质」，",
                "每个角色一段 40~70 字的描述。这段描述会被拿去生成角色定妆照，也是全片人物长相一致的唯一依据 —— ",
                "所以特征必须具体、可画（不要写「很漂亮」「有气质」这种没法画的话）。",
            ]
        )
    return "\n".join(
        [
            head,
            "",
            "只输出 JSON，不要任何解释、不要 markdown 代码块。JSON 格式：",
            '{"title":"剧名","characters":[{"name":"角色名","desc":"外貌与服装的详细描述"}],'
            '"shots":[{"scene":"这个镜头的画面描述","dialogue":"台词或空字符串","characters":["出现在这个镜头里的角色名"]}]}',
            "",
            "各字段要求：",
            "- title：剧名，12 字以内。",
            char_rule,
            f"- shots：正好 {shot_count} 个镜头，按剧情顺序排列。",
            "  · scene：这个镜头的画面描述，50~90 字。要写清楚「在哪、谁在做什么、什么景别（近景/中景/全身）、什么动作、什么表情」。",
            "    每个镜头都必须是独立的、可单独画出来的一幅画面。",
            "  · dialogue：这个镜头里角色说的话，15 字以内；没有台词就填空字符串。",
            "  · characters：这个镜头里出现的角色名，必须是上面 characters 里出现过的名字。",
            "",
            "硬性要求：",
            "- 剧情要有起承转合，镜头之间要有连贯性，不要 12 个互不相关的画面。",
            "- 不要写任何真实人名、品牌名、地名。",
            "- 不要出现暴力、色情、政治相关内容。",
            f"- 全片统一风格：{style}。不要把风格写进每个 scene，scene 只写画面内容。",
            f"\n用户给的题材是：「{idea}」。请围绕它编。" if idea else "\n用户没给题材，你自由发挥一个有趣的小剧场。",
        ]
    )


def extract_json(text: str | None) -> dict | list | None:
    """把模型返回的文本里那段 JSON 抠出来（免费小模型经常带 markdown 围栏或前后废话）"""
    raw = str(text or "").strip()
    raw = re.sub(r"^```(?:json)?", "", raw, flags=re.I)
    raw = re.sub(r"```$", "", raw).strip()
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        pass  # 继续往下试
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(raw[start : end + 1])
        except (ValueError, TypeError):
            return None
    return None


def explain_http_error(status: int, text: str | None) -> str:
    """各家报错文案差别很大，统一成人话（与抖音标签同一套口径）"""
    detail = re.sub(r"\s+", " ", str(text or ""))[:200]
    tail = f"：{detail}" if detail else ""
    if status in (401, 403):
        return f"API Key 不对，或者这个 Key 没有调用该模型的权限（HTTP {status}）{tail}"
    if status == 404:
        return f"接口地址或模型 ID 不对，检查这两项有没有填错（HTTP 404）{tail}"
    if status == 429:
        return "调用太频繁，或者免费额度用完了，等一会儿再试（HTTP 429）"
    if status >= 500:
        return f"模型服务端出错，稍后再试（HTTP {status}）"
    return f"写剧本失败（HTTP {status}）{tail}"


def ref_data_uri(ref: dict | None) -> str:
    """把一张参考图读成 data URI；文件不在了返回空串（不抛错，调用方跳过即可）"""
    try:
        if ref and ref.get("url"):
            return ""  # 外网链接图没法直接塞进 messages，跳过
        local = str((ref or {}).get("localPath") or "")
        if not local or not Path(local).exists():
            return ""
        ext = Path(local).suffix.lower()
        mime = (ref or {}).get("mimeType") or REF_MIME_TYPES.get(ext) or "image/png"
        import base64

        return f"data:{mime};base64,{base64.b64encode(Path(local).read_bytes()).decode('ascii')}"
    except (OSError, ValueError):
        return ""


async def post_chat(cfg: dict, body: dict, timeout: int = SCRIPT_TIMEOUT_MS) -> str:
    """一次 OpenAI 兼容的对话请求，返回模型回的那段文本。

    抽出来是为了让「节点画布 · AI 编导」复用同一份配置和同一套报错口径 ——
    两个入口都在写剧本，用户看到的错误提示就该一模一样。
    """
    base_url = str(cfg.get("baseUrl") or "")
    url = base_url if re.search(r"/chat/completions$", base_url) else f"{base_url}/chat/completions"
    deadline = now_ms() + timeout

    # 上游偶发 500 / 网络抖动很常见（实测智谱在长分镜 prompt 下会间歇性 5xx），
    # 一次就抛给用户等于让整条编导白等一两分钟。5xx 与网络错误重试两次，
    # 4xx（Key 不对、地址不对、内容违规）重试也没用，直接报。
    last_err: Exception | None = None
    res: httpx.Response | None = None
    for attempt in range(3):
        if attempt > 0:
            await asyncio.sleep(1.2 if attempt == 1 else 3.0)
        left_ms = deadline - now_ms()
        if left_ms < 5000:
            raise ValueError(f"模型一直没有响应（超过 {round(timeout / 1000)} 秒），换个模型或稍后再试")
        try:
            async with httpx.AsyncClient(timeout=max(1.0, left_ms / 1000)) as client:
                res = await client.post(
                    url,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {cfg.get('apiKey') or ''}",
                    },
                    content=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                )
        except httpx.TimeoutException:
            raise ValueError(
                f"模型一直没有响应（超过 {round(timeout / 1000)} 秒），换个模型或稍后再试"
            ) from None
        except httpx.HTTPError as err:
            last_err = err
            res = None
            continue  # 连不上：网络抖动，重试
        if res.status_code >= 500:
            last_err = ValueError(explain_http_error(res.status_code, res.text))
            res = None
            continue  # 上游 5xx：重试
        break

    if res is None:
        raise last_err or ValueError("模型接口没有响应，稍后再试")

    text = res.text
    if res.status_code >= 400:
        raise ValueError(explain_http_error(res.status_code, text))

    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        raise ValueError("模型返回的不是标准 JSON，换个模型再试") from None
    if isinstance(payload, dict) and payload.get("error"):
        err_obj = payload["error"]
        raise ValueError((err_obj or {}).get("message") if isinstance(err_obj, dict) else str(err_obj) or "模型返回了错误")

    content = ((payload.get("choices") or [{}])[0].get("message") or {}).get("content") if isinstance(payload, dict) else ""
    if isinstance(content, str):
        answer = content
    elif isinstance(content, list):
        answer = "".join(str((c or {}).get("text") or "") for c in content)
    else:
        answer = ""
    if not answer.strip():
        raise ValueError("模型没返回内容，换个模型再试")
    return answer


async def chat_script(
    *,
    prompt: str,
    temperature: float = 0.8,
    timeout: int = SCRIPT_TIMEOUT_MS,
    images: list[str] | None = None,
) -> str:
    """纯文本对话：给「AI 编导」这类只要文字结构的功能用（不传图）"""
    cfg = script_model_config()
    if not cfg["baseUrl"] or not cfg["model"]:
        raise ValueError("还没有配置写剧本的模型（要支持 OpenAI 兼容的对话接口），先去「视频生成 → 分镜」里填一个")
    if not cfg["apiKey"]:
        raise ValueError("写剧本的模型还没有填 API Key")

    usable = [u for u in (images or []) if isinstance(u, str) and u.startswith("data:image/")]
    # 有图就发多模态 content 数组；没图保持原来的纯文本，兼容不支持视觉的模型
    if usable:
        content: object = [
            {"type": "text", "text": str(prompt or "")},
            *[{"type": "text", "text": f"第 {i + 1} 张人物参考图："} for i, _ in enumerate(usable)],
            *[{"type": "image_url", "image_url": {"url": u}} for u in usable],
        ]
    else:
        content = str(prompt or "")

    return await post_chat(cfg, {"model": cfg["model"], "messages": [{"role": "user", "content": content}], "temperature": temperature}, timeout)


def collect_photo_refs(photo_ids: list | None, max_count: int = 4) -> list[dict]:
    """把「参考图库里的 id」收成能直接发给模型的图。

    只认真实存在、且本地文件还在的 —— 少了就少给一张，别让整条链路挂掉。
    返回 `[{ref, dataUri}]`。
    """
    out: list[dict] = []
    for ref_id in (photo_ids or [])[:max_count] if isinstance(photo_ids, list) else []:
        ref = find_image_ref(str(ref_id))
        if not ref:
            continue
        data_uri = ref_data_uri(ref)
        if not data_uri:
            continue
        out.append({"ref": ref, "dataUri": data_uri})
    return out


async def write_script(
    *,
    idea: str | None = None,
    shot_count: int | None = None,
    style: str | None = None,
    photo_ids: list | None = None,
) -> dict:
    """让模型编一份剧本，返回一份已经落库的 storyboard。

    注意：这里只出「文字」，不生成任何图片 —— 出图是用户确认剧本之后再点的事。

    `photo_ids` 是「人物参考图」的图库 id 数组（可选）。给了就走多模态：
    把图一起发给视觉模型，让它照着图里的人写角色描述，并把图登记成角色的定妆照 ——
    这样人物完全由用户指定，不用 AI 凭空编长相。
    """
    cfg = script_model_config()
    if not cfg["baseUrl"] or not cfg["model"]:
        raise ValueError("还没配置写剧本的模型，点分镜面板上的「剧本模型」填一个（支持 OpenAI 兼容的对话模型）")
    if not cfg["apiKey"]:
        raise ValueError("写剧本的模型还没有填 API Key")

    # ---- 收人物图：只认图库里真实存在、且本地文件还在的 ----
    photos: list[dict] = []
    for ref_id in (photo_ids or [])[:MAX_CHARACTERS] if isinstance(photo_ids, list) else []:
        ref = find_image_ref(str(ref_id))
        if not ref:
            continue
        data_uri = ref_data_uri(ref)
        if data_uri:
            photos.append({"ref": ref, "dataUri": data_uri})

    want = max(MIN_SHOTS, min(MAX_SHOTS, _as_int(shot_count) or 6))
    use_style = str(style or "").strip() or DEFAULT_STYLE

    # 有人物图时按图片张数定角色数：一张图一个角色，最直观
    character_count = len(photos) or 2
    prompt_text = _build_script_prompt(
        idea=idea,
        shot_count=want,
        style=use_style,
        character_count=character_count,
        has_photos=bool(photos),
    )

    # 有图就发多模态 content 数组；没图保持原来的纯文本，兼容不支持视觉的模型
    if photos:
        message_content: object = [
            {"type": "text", "text": prompt_text},
            *[{"type": "text", "text": f"第 {i + 1} 张人物参考图："} for i, _ in enumerate(photos)],
            *[{"type": "image_url", "image_url": {"url": p["dataUri"]}} for p in photos],
        ]
    else:
        message_content = prompt_text

    body = {
        "model": cfg["model"],
        "messages": [{"role": "user", "content": message_content}],
        "temperature": 0.8,  # 编故事需要点发挥，比识图调高
    }

    answer = await post_chat(cfg, body)

    parsed = extract_json(answer)
    if not parsed or not isinstance(parsed, dict):
        raise ValueError("模型没有按要求给出 JSON 剧本，换个模型再试（免费小模型经常不守格式）")

    # ---- 规整角色 ----
    raw_chars = parsed.get("characters") if isinstance(parsed.get("characters"), list) else []
    characters: list[dict] = []
    for c in raw_chars:
        c = c or {}
        name = str(c.get("name") or "").strip()[:20]
        desc = str(c.get("desc") or "").strip()[:300]
        if name and desc:
            characters.append({"name": name, "desc": desc})
    characters = characters[:MAX_CHARACTERS]
    if not characters:
        raise ValueError("模型没有给出可用的角色设定（缺 name 或 desc），换个模型再试")

    # 用户给了人物图：按顺序把图直接挂成角色的定妆照 —— 这一步跳过「AI 生成定妆照」，
    # 人物就是用户给的那个人，之后每个镜头出图都拿它当参考图。
    photo_by_index = [p["ref"] for p in photos]
    for index, character in enumerate(characters):
        ref = photo_by_index[index] if index < len(photo_by_index) else None
        if not ref:
            continue
        character["refId"] = ref.get("id")
        character["refUrl"] = ref.get("localUrl")
        character["fromPhoto"] = True  # 界面上标一下「这是你给的人物图」

    # ---- 规整镜头 ----
    raw_shots = parsed.get("shots") if isinstance(parsed.get("shots"), list) else []
    valid_names = {c["name"] for c in characters}
    shots: list[dict] = []
    for s in raw_shots:
        s = s or {}
        scene = str(s.get("scene") or "").strip()[:500]
        if not scene:
            continue
        shots.append(
            {
                "scene": scene,
                "dialogue": str(s.get("dialogue") or "").strip()[:100],
                # 只保留确实在角色表里的名字：模型有时会编出个没定义的角色
                "characters": [
                    str(n or "").strip()
                    for n in (s.get("characters") if isinstance(s.get("characters"), list) else [])
                    if str(n or "").strip() in valid_names
                ],
            }
        )
    if not shots:
        raise ValueError("模型没有给出可用的镜头画面描述，换个模型再试")

    board = new_storyboard(
        title=str(parsed.get("title") or "").strip()[:30] or "未命名小剧场",
        style=use_style,
        characters=characters,
        shots=shots,
    )
    store.storyboards.insert(0, board)
    store.mark_dirty("storyboards")
    store.save()
    store.flush()
    return board


def _as_int(value) -> int:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return 0
    if num != num:  # NaN
        return 0
    return int(num)


# ============================================================ 出定妆照


def _pick_image_model() -> dict | None:
    """没指定模型时挑一个：优先能图生图的（分镜后面要靠它），其次第一个启用的"""
    enabled = [m for m in store.image_models if m.get("enabled") and m.get("model")]
    for model in enabled:
        if model.get("supportsImageInput"):
            return model
    return enabled[0] if enabled else None


def cast_character(board: dict, character_name: str, *, model_id: str | None = None, size: str | None = None) -> dict:
    """给某个角色出一张定妆照。

    提示词 = 画风 + 角色外貌 + 一句「正面全身/半身定妆照」的取景要求。
    这一步不挂参考图 —— 它就是全片人物长相的「源头」，后续所有镜头都参考它。
    """
    character = next((c for c in (board.get("characters") or []) if c.get("name") == character_name), None)
    if not character:
        raise ValueError(f"剧本里没有叫「{character_name}」的角色")

    model = find_image_model(model_id) if model_id else _pick_image_model()
    if not model:
        raise ValueError("还没有可用的图片模型，先去「图片生成 → 模型管理」加一个")

    prompt = _join_sentence(
        [
            board.get("style"),
            f"角色定妆照：{character.get('desc')}",
            "正面半身特写，看着镜头，表情自然，背景干净简洁，人物占据画面主体，五官清晰",
            "这是同一个角色的标准形象参考图，请保持外貌特征稳定",
        ]
    )

    task = create_image_task(
        model_id=model.get("id"),
        prompt=prompt,
        size=size or model.get("size") or "",
        count=1,
        ref_ids=[],
    )
    # 记下是哪条任务在出这张定妆照，前端据此显示进度
    task["storyboardId"] = board.get("id")
    task["storyboardRole"] = character.get("name")
    character["refTaskId"] = task.get("id")
    board["status"] = "casting"
    store.mark_dirty("image_tasks")
    store.mark_dirty("storyboards")
    store.save()
    store.flush()
    return task


def _join_sentence(parts: list) -> str:
    """用「，」拼句，但要先去掉各段自己的句末标点，否则会出现「…清晰。，这是…」这种叠标点"""
    cleaned = [re.sub(r"[。，、；,;]+$", "", str(p or "").strip()) for p in parts]
    return "，".join(p for p in cleaned if p) + "。"


# ============================================================ 批量出图


def draw_shots(board: dict, *, model_id: str | None = None, size: str | None = None) -> list[dict]:
    """为剧本里所有「还没出图」的镜头批量建图片任务。

    每个镜头的参考图 = 剧本里各角色的形象图（用户给的人物图，或 AI 生成的定妆照）。
    一个角色都没有形象图就不让出图 —— 那样画出来的人会每张都不一样。

    返回建好的任务数组；任务本身由 image-jobs 的串行队列依次执行。
    """
    model = find_image_model(model_id) if model_id else _pick_image_model()
    if not model:
        raise ValueError("还没有可用的图片模型，先去「图片生成 → 模型管理」加一个")

    cast = [c for c in (board.get("characters") or []) if c.get("refId")]
    if not cast:
        raise ValueError("还没有任何角色形象图（用户给的人物图或定妆照），先补上再出分镜图")

    ref_ids = [c.get("refId") for c in cast]
    created: list[dict] = []
    for shot in board.get("shots") or []:
        # 已经出过图（含正在出的）就跳过：重复点「批量出图」不该重复扣费
        if shot.get("imageTaskId"):
            continue

        # 这个镜头里出现的角色，把各自的形象描述也写进提示词 ——
        # 参考图负责「长得像」，文字描述负责「别把服装/发色画跑偏」，两者一起上最稳。
        in_shot = []
        for name in shot.get("characters") or []:
            found = next((c for c in (board.get("characters") or []) if c.get("name") == name), None)
            if found:
                in_shot.append(f"{found.get('name')}：{found.get('desc')}")

        # 画风 + 角色 + 镜头画面：先削掉各段句末标点再拼，避免「…，。，」这类叠标点
        prompt = _join_sentence([board.get("style"), "；".join(in_shot), shot.get("scene")])
        task = create_image_task(
            model_id=model.get("id"),
            prompt=prompt,
            size=size or model.get("size") or "",
            count=1,
            ref_ids=ref_ids,
        )
        task["storyboardId"] = board.get("id")
        task["storyboardShot"] = shot.get("index")
        shot["imageTaskId"] = task.get("id")
        shot["note"] = ""
        created.append(task)

    if created:
        board["status"] = "drawing"
        board["error"] = ""
        store.mark_dirty("image_tasks")
        store.mark_dirty("storyboards")
        store.save()
        store.flush()
    return created


# ============================================================ 快照


def _image_task_brief(task_id: str | None) -> dict | None:
    """一条图片任务压缩成界面要用的几个字段（分镜卡片上只显示状态和缩略图）"""
    if not task_id:
        return None
    task = store.find("image_tasks", id=task_id)
    if not task:
        return None
    images = task.get("images") or []
    image = images[0] if images else {}
    return {
        "id": task.get("id"),
        "status": task.get("status"),
        "error": task.get("error") or "",
        "modelName": task.get("modelName") or "",
        "imageId": image.get("id") or "",
        "url": image.get("localUrl") or image.get("remoteUrl") or "",
        "createdAt": task.get("createdAt"),
    }


def storyboard_snapshot(board: dict) -> dict:
    """下发给前端的剧本快照。

    每次都从 `image_tasks` 现查图片状态，而不是存一份快照在剧本里 ——
    任务会被重试、会被裁剪，存快照就会和真实结果对不上。
    """
    return {
        "id": board.get("id"),
        "title": board.get("title"),
        "style": board.get("style"),
        "status": board.get("status"),
        "error": board.get("error") or "",
        "createdAt": board.get("createdAt"),
        "characters": [
            {
                "name": c.get("name"),
                "desc": c.get("desc"),
                "refUrl": c.get("refUrl") or "",
                "refTaskId": c.get("refTaskId") or "",
                "cast": _image_task_brief(c.get("refTaskId")),
                "note": c.get("note") or "",
                # 形象图是用户自己给的人物图（True）还是 AI 生成的定妆照（False）
                "fromPhoto": bool(c.get("fromPhoto")),
            }
            for c in (board.get("characters") or [])
        ],
        "shots": [
            {
                "index": s.get("index"),
                "scene": s.get("scene"),
                "dialogue": s.get("dialogue"),
                "characters": s.get("characters") or [],
                "note": s.get("note") or "",
                "image": _image_task_brief(s.get("imageTaskId")),
                "videoTaskId": s.get("videoTaskId") or "",
            }
            for s in (board.get("shots") or [])
        ],
    }


def find_storyboard(board_id: str | None) -> dict | None:
    if not board_id:
        return None
    return store.find("storyboards", id=board_id)


def all_storyboard_snapshots() -> list[dict]:
    """所有剧本的快照（列表用，按创建时间倒序）"""
    rows = sorted(store.storyboards, key=lambda b: -(int(b.get("createdAt") or 0)))
    return [storyboard_snapshot(b) for b in rows]


def remove_storyboard(board: dict) -> None:
    """删一份剧本：只删剧本本身，已经生成出来的图片/视频留在各自模块里，不跟着删"""
    rows = store.storyboards
    for index, item in enumerate(rows):
        if item.get("id") == board.get("id"):
            del rows[index]
            break
    store.mark_dirty("storyboards")
    store.save()
    store.flush()


def harvest_cast(board: dict) -> bool:
    """把已经出好的定妆照「收」进角色记录。

    由 `/api/state` 的组装阶段调用：定妆照任务成功后，把结果图登记成参考图，
    之后每个镜头出图就拿这个 `refId` 当参考图。
    """
    changed = False
    for character in board.get("characters") or []:
        if character.get("refId") or not character.get("refTaskId"):
            continue
        task = find_image_task(character.get("refTaskId"))
        if not task:
            continue
        images = task.get("images") or []
        if task.get("status") == "success" and images:
            image = images[0]
            try:
                ref = create_ref_from_image(task, image)
                character["refId"] = ref.get("id")
                character["refUrl"] = ref.get("localUrl")
                character["refImageId"] = image.get("id")
                changed = True
            except (ValueError, OSError) as err:
                character["refTaskId"] = ""
                character["note"] = f"定妆照登记失败：{err}"
                changed = True
        elif task.get("status") in ("failed", "cancelled"):
            # 出图失败就清掉任务 id，界面上那个角色回到「未定妆」，可以重出
            character["refTaskId"] = ""
            character["note"] = task.get("error") or "定妆照生成失败"
            changed = True
    if changed:
        store.mark_dirty("storyboards")
        store.mark_dirty("image_refs")
        store.save()
    return changed


def sync_storyboards() -> None:
    """每次 `/api/state` 组装时调一次：把已完成的定妆照收进角色记录"""
    changed = False
    for board in store.storyboards:
        if harvest_cast(board):
            changed = True
    if changed:
        store.mark_dirty("storyboards")
        store.save()
