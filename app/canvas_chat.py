"""节点画布 · AI 聊天（对话式编导）—— 逐函数复刻 `server/canvas-chat.js`。

跟 `canvas_director.py` 的分工：
  · canvas_director 是「一段文字 → 一份分镜」，纯函数，不谈上下文；
  · 这里是「聊天」，负责判断用户这一句话想干嘛，再决定要不要叫上面那个。

为什么拆两步：
  分镜 prompt 里塞了大量字段规范（scene 要 50~90 字、motion 怎么写、characters 不能漏），
  这些跟「聊天」混在一条 prompt 里，小模型两件事都做不好 —— 要么 reply 变成说明文，
  要么镜头字段缺一半。所以先让模型只做「判意图 + 回一句话」，真要铺分镜再走原来那条
  已经调好的链路。代价是多一次调用，换来的是分镜质量不打折。

状态全在画布上：这里不落库、不建任务，只返回「回什么话 + 要做什么」。
"""

from __future__ import annotations

import re
import time
from typing import Any

from .canvas_director import direct_storyboard, director_engine, is_script_like
from .providers.doubao_bridge import ask_doubao
from .storyboard import chat_script, extract_json

#: 只带最近几轮：小模型的上下文窗口小，塞多了反而忘掉当前画布状态
MAX_TURNS = 8

#: 整个 `/api/canvas/chat` 请求的总预算。
#:
#: 前端 `public/nodecanvas.js` 的 CHAT_TIMEOUT_MS 是 300 秒，到点就 abort，
#: 用户看到的是「signal timed out」。服务端必须在这之前把话说出来 ——
#: 所以这里给 280 秒，并把它**切成几段**分给「分类调用」和「拆分镜」：
#: 一次聊天里最多会有 1 次分类调用（~90 秒）+ 1 条完整编导链路（~225 秒），
#: 串起来早就爆了。分类调用先跑，剩下的全给编导。
CHAT_BUDGET_MS = 280000

#: 分类调用最多用掉这么多，剩下的必须留给编导（不然分镜还没开始就被掐）。
#: 分类的 prompt 很短（只回一个小 JSON），但实测 glm-4.5-flash 带 thinking 时
#: 也要 30~90 秒；给 90 秒，偶发 5xx 还有 postChat 的三次重试兜着。
#: 留 180 秒给编导，才够拆 6 镜。
#: 另外：用户直接贴剧本时不会走这条（is_script_like 直接进编导）。
CLASSIFY_MAX_MS = 90000

INTENTS = {"plan", "revise", "run", "chat"}

#: 「开始生成」这句话不需要模型来判断 —— 说跑就跑。
#:
#: 之前完全交给模型，glm-4v-flash 会看着历史里那条「第 2 镜改成雨夜」接着往下改，
#: 把「开始生成」当成又一轮修改。这类指令明确、结果唯一，本地判掉更快也更稳。
#:
#: 正则故意收得很紧（整句就是这几个词，或者后面只跟语气词 / 数量），
#: 否则「生成的图不对」这种句子也会被当成「开始生成」。
RUN_RE = re.compile(
    r"^(一键生成|开始生成|生成|开始|开跑|跑起来|出图|出视频|开工|执行)"
    r"(吧|啊|一下|呗|呢|全部|所有|全部分镜|所有镜头|分镜|镜头|图片|视频|[！!。.~～ ])*$"
)

#: edits 翻译成人话时用的字段中文名
FIELD_LABEL = {"scene": "画面", "motion": "运镜", "dialogue": "台词", "duration": "时长"}

#: 模型常把字段名写进内容（「画面：…」「scene: …」），拼到提示词里会变成噪音
_FIELD_NAME_RE = re.compile(r"^(画面|场景|内容|scene|prompt)\s*[:：]\s*", re.I)

#: 「…中景｜运镜：镜头固定｜时长：10 秒｜出场：阿岚｜还没出图」这种元信息尾巴
_TRAILER_RE = re.compile(r"[，,；;]\s*(运镜|时长|出场|台词|角色|镜头运动)\s*[:：][\s\S]*$")


def _text(value: Any, max_len: int) -> str:
    """`text(v, max)` —— JS 的 `String(v ?? '').trim().slice(0, max)`"""
    if value is None:
        return ""
    return str(value).strip()[:max_len]


def now_ms() -> int:
    return int(time.time() * 1000)


def _clean(value: Any) -> str:
    """`String(s || '').replace(/[\\s\\p{P}]+/gu, '')`"""
    return re.sub(r"[\s\W_]+", "", str(value or ""), flags=re.UNICODE)


def containment(needle: Any, hay: Any) -> float:
    """中文按 2 字滑窗比「包含度」：needle 的字对里有多少出现在 hay 里（0~1）"""
    a = _clean(needle)
    b = _clean(hay)
    if not a or not b:
        return 0.0
    grams = {a[i : i + 2] for i in range(len(a) - 1)}
    hit = sum(1 for g in grams if g in b)
    return hit / len(grams) if grams else 0.0


# ------------------------------------------------------------------ 本地判定


def mentioned_shots(said: Any) -> set[int]:
    """用户点了名的那几镜：「第 3 镜」「镜头 3」「3 号镜」都认"""
    found: set[int] = set()
    for hit in re.finditer(r"第\s*(\d{1,2})\s*镜|镜头\s*(\d{1,2})|(\d{1,2})\s*号镜", str(said or "")):
        value = hit.group(1) or hit.group(2) or hit.group(3)
        try:
            num = int(value)
        except (TypeError, ValueError):
            continue
        if num >= 1:
            found.add(num)
    return found


def is_plan_directive(said: Any) -> bool:
    """「把它拆成分镜」这种指代句：句子里没有剧本，原文在前一轮。

    这种话不用问模型 —— 实测 glm-4v-flash 会把 prompt 里的占位符
    「要拆分镜的原文」原样抄进 script，还会把 JSON 输出成 markdown 代码块导致解析失败。
    本地判掉更快也更准。点名了「第 N 镜」的一律不算（那是改分镜，不是重新铺）。
    """
    s = str(said or "").strip()
    if not s or len(s) > 20 or mentioned_shots(s):
        return False
    if "分镜" in s:
        return not re.search(r"改|换|去掉|重写|删", s)
    return bool(
        re.match(r"^(把|将|就|照|按|拿|用)", s)
        and re.search(r"拆|分|做|拍|铺", s)
        and not re.search(r"改|换|去掉|删", s)
    )


def find_earlier_script(history: list[dict], last_user: dict | None) -> str:
    """前几轮里最长的那段用户消息（= 大概率就是剧本原文）"""
    for m in reversed(history):
        if m.get("role") != "user" or m is last_user:
            continue
        if len(str(m.get("content") or "").strip()) >= 20:
            return str(m["content"]).strip()
    return ""


def pick_script(model_script: Any, history: list[dict], last_user: dict | None) -> str:
    """「要拆分镜的原文」绝不能直接信模型给的。

    实测 glm-4v-flash 会自由发挥：用户说「女神起床伸懒腰、闺蜜做饭、搂腰脸红」，
    它在 script 字段里编了一个「雨夜奇遇」的故事，拿去一拆，画布上铺出来的
    就完全是另一个世界（用户看到的《迷雾中的舞者》就是这么来的）。

    所以 script 只认「和哪条用户消息对得上」的原文：
    - 模型抄的是历史里某条用户消息（≥50% 字对重合）→ 用那条**用户的原话**，不用模型的改写；
    - 对不上（= 编的）→ 用用户最后说的一句话。
    任何情况下都不把模型写的文字当剧本。
    """
    said = str((last_user or {}).get("content") or "").strip()
    got = str(model_script or "").strip()

    # 用户这句只是「把它拆成分镜」这种指代 —— 真正的原文在前面几轮里，直接捞那段
    if is_plan_directive(said):
        older = find_earlier_script(history, last_user)
        if older:
            return older

    if not got:
        return said

    best_msg = said
    best_score = containment(got, said)
    # 从新到旧找：同样对得上时，优先离得近的那条
    for m in reversed(history):
        if m.get("role") != "user" or not m.get("content") or m is last_user:
            continue
        score = containment(got, m.get("content"))
        if score > best_score:
            best_msg = str(m["content"])
            best_score = score
    # 对得上 → 用那条用户原话；对不上（模型自己编的）→ 用用户最后这句
    return best_msg if best_score >= 0.5 else said


# ------------------------------------------------------------------ 文本清洗


def trim_echo(reply: Any, said: Any) -> str:
    """模型爱把用户原话当成回复的开头：「第 2 镜改成雨夜，铺好了，3 个镜头。」

    聊天气泡里用户刚说过的话再重复一遍很蠢，直接剥掉。
    """
    said_s = str(said or "").strip()
    reply_s = str(reply or "").strip()
    if len(said_s) < 4 or not reply_s.startswith(said_s):
        return reply_s
    rest = reply_s[len(said_s) :]
    rest = re.sub(r"^[，,。.、；;：:\s]+", "", rest)
    return rest or "好。"


def strip_field_name(value: Any) -> str:
    return _FIELD_NAME_RE.sub("", str(value or ""))


def strip_trailer(value: Any) -> str:
    """更糟的一种：模型直接抄 describe_scene 里那一行摘要，
    「…中景｜运镜：镜头固定｜时长：10 秒｜出场：阿岚｜还没出图」整串塞进画面描述。
    没有竖线的时候它会改写成「…近景，运镜：镜头固定，时长：10 秒」——
    竖线之后、或者带冒号的元信息字段之后，都不是画面本身，截断掉。
    """
    line = str(value or "").split("｜")[0]
    return _TRAILER_RE.sub("", line).strip()


# ------------------------------------------------------------------ 画布现状


def describe_scene(scene: Any) -> str:
    """把画布现状说给模型听：它只能看见这段文字，看不见节点"""
    if not isinstance(scene, dict) or not isinstance(scene.get("shots"), list) or not scene["shots"]:
        return "画布上还没有分镜。"

    shots = scene["shots"]
    lines = [
        f"画布上已经有一份分镜{('《' + str(scene['planTitle']) + '》') if scene.get('planTitle') else ''}，"
        f"共 {len(shots)} 个镜头。"
    ]
    # 成片结构要讲清楚：模型看不见节点，不说明白它会以为「一个镜头配一段视频」
    lines.append(
        f"出片结构：图 → 视频 → 图 → 视频 … 串起来的，所以 {len(shots)} 个镜头有 "
        f"{max(0, len(shots) - 1)} 段视频；"
        "第 N 段视频从「第 N 镜的图」走到「第 N+1 镜的图」（前一张是首帧、后一张是尾帧），"
        "第一镜前面没有镜头，不单独出视频。"
    )
    if scene.get("ratio"):
        lines.append(f"画幅：{scene['ratio']}。")
    if scene.get("style"):
        lines.append(f"画风：{scene['style']}。")

    casts = scene.get("characters") if isinstance(scene.get("characters"), list) else []
    if casts:
        lines.append("角色：" + "；".join(
            f"{c.get('name')}{('（' + str(c['desc']) + '）') if c.get('desc') else ''}"
            for c in casts
        ) + "。")
    lines.append("")
    for shot in shots:
        if not isinstance(shot, dict):
            continue
        parts = [f"第 {shot.get('index')} 镜"]
        if shot.get("scene"):
            parts.append(f"画面：{shot['scene']}")
        if shot.get("endState"):
            parts.append(f"收尾：{shot['endState']}")
        if shot.get("motion"):
            parts.append(f"运镜：{shot['motion']}")
        if shot.get("dialogue"):
            parts.append(f"台词：{shot['dialogue']}")
        if shot.get("duration"):
            parts.append(f"时长：{shot['duration']} 秒")
        if shot.get("characters"):
            parts.append(f"出场：{shot['characters']}")
        parts.append("已有图" if shot.get("hasImage") else "还没出图")
        lines.append("- " + "｜".join(str(p) for p in parts))
    return "\n".join(lines)


def build_prompt(*, messages: list[dict], scene: Any, prefs: Any) -> str:
    want = max(2, min(12, round(float((prefs or {}).get("shotCount") or 6))))
    lines = [
        "你是一个嵌在视频创作画布右侧的 AI 编导。用户直接跟你说话，你判断他这一句想干什么，然后回一句话。",
        "",
        "可能的意思只有这四种，必须选一个：",
        "- plan：用户给了剧本、故事梗概、或者一句题材，要你拆成分镜铺到画布上。",
        "- revise：画布上已经有分镜，用户要改其中某些镜头（改画面、改运镜、改台词、改长短）。",
        "- run：用户让你开始出图 / 出视频。",
        "- chat：其它提问、闲聊、或者只是问你能干什么。",
        "",
        "当前画布：",
        describe_scene(scene),
        "",
        "只输出 JSON，不要解释，不要 markdown 代码块：",
        '{"reply":"回给用户的话","intent":"plan","script":"从用户消息里原样摘的剧本（没有就填空串）","shotCount":6,"edits":[]}',
        "",
        "字段要求：",
        "- reply：中文，**最多 3 句、60 字以内**，像同事说话，直接给结论。",
        "  · 不要复述用户的话，不要写「好的，我将…」这种客套，**不要罗列分镜内容**（分镜已经铺到画布上了，用户自己看得见）。",
        "  · 只说一句「铺好了，X 个镜头」这种结论，需要提醒的补半句。",
        "- intent：只能是 plan / revise / run / chat 之一。",
        "- script：intent=plan 时必填，把用户给的那段内容**原文照抄**下来（不要改写、不要摘要），最长 8000 字。其它意图留空串。",
        "- shotCount：intent=plan 时填镜头数，只能填 2~12 的整数。",
        f"  · 用户明说了「拆成 4 个镜头」就填 4；**没说就填 {want}（右边参数面板当前的值，不是示例里的数字）**，不要照抄示例。",
        "- edits：intent=revise 时必填，数组，每项一个要改的镜头：",
        '  {"shotIndex":3,"scene":"改后的画面描述","motion":"改后的运镜","endState":"改后的收尾画面","dialogue":"改后的台词","duration":5}',
        "  · shotIndex 是第几镜（从 1 开始，看上面画布里的编号）。",
        "  · **只写真正要改的字段，没提到的一律不要出现**（不要填 null，也不要填原值）。",
        "  · 不要把没被点名的镜头写进 edits。用户说「第 3 镜」就只改第 3 镜。",
        "  · 画面描述写具体（在哪、谁、做什么、光线、景别），50~90 字；运镜写「什么在动、怎么动」，15~40 字。",
        "  · duration 只能是 5 或 10。",
        "",
        "判断规则：",
        "- **只看最后一条用户消息**。前面几轮里出现过的修改早就执行完了，不要再重复一遍。",
        "- 用户贴了一段剧情 / 故事 / 场景描述（哪怕只有一句话），或者明说「拆成分镜」「按这个拍」→ plan。",
        "- 画布已经有分镜，用户说的是「改成…」「换个…」「快一点」「加一镜」→ revise。",
        "- 用户说「生成」「开始」「跑起来」「出图」→ run。",
        "- 拿不准就选 chat，并问一句他想干什么。",
        "",
        "用户说的话（由旧到新）：",
        *[f"{'用户' if m.get('role') == 'user' else '你'}：{_text(m.get('content'), 4000)}" for m in messages],
    ]
    return "\n".join(lines)


# ------------------------------------------------------------------ 意图兜底


def guess_intent(answer: Any, last_user: Any) -> str:
    """模型没给 intent（或给了个没见过的值）时，按关键词猜一下，别让整条链路瘫掉"""
    said = str(last_user or "").strip()
    both = f"{answer}\n{said}"
    # 「生成的图不对」也以「生成」开头，所以只在短句上认这条
    if len(said) <= 12 and (
        re.match(r"^(生成|开始|开跑|跑起来|出图|出视频|一键)", said)
        or re.search(r"开始(生成|出图|跑)", both)
    ):
        return "run"
    if re.search(r"改成|换个|换一|去掉|加上|快一点|慢一点|调一下|重写", said):
        return "revise"
    if len(said) >= 30:
        return "plan"
    return "chat"


def describe_edits(edits: list[dict]) -> str:
    """edits 翻译成人话：「改好了：第 2 镜的画面、运镜；第 3 镜的台词。」——比让模型复述可靠"""
    parts = []
    for edit in edits:
        fields = [FIELD_LABEL.get(k, k) for k in edit.keys() if k != "shotIndex"]
        parts.append(f"第 {edit.get('shotIndex')} 镜的{'、'.join(fields)}")
    # 只有画面真改过，旧图才作废；只改台词、时长的时候说这句会让人以为图没了
    tail = "画面改过的镜头会清掉旧图，点「一键生成」重出。" if any(e.get("scene") for e in edits) else ""
    return f"改好了：{'；'.join(parts)}。{tail}"


def sanitize_edits(raw: Any) -> list[dict]:
    """把模型给的 edits 收干净：只要认得出的字段，shotIndex 必须是正整数"""
    out: list[dict] = []
    for item in (raw if isinstance(raw, list) else []):
        if not isinstance(item, dict):
            continue
        try:
            shot_index = round(float(item.get("shotIndex")))
        except (TypeError, ValueError):
            continue
        if shot_index < 1:
            continue
        edit: dict[str, Any] = {"shotIndex": int(shot_index)}
        scene = _text(strip_trailer(strip_field_name(item.get("scene")), ), 500)
        motion = _text(strip_trailer(strip_field_name(item.get("motion"))), 200)
        # 收尾画面：这一镜动作做完时画面停在哪（它同时是下一段视频的尾帧）
        end_state = _text(strip_trailer(strip_field_name(item.get("endState"))), 200)
        dialogue = _text(item.get("dialogue"), 100)
        if scene:
            edit["scene"] = scene
        if motion:
            edit["motion"] = motion
        if end_state:
            edit["endState"] = end_state
        # 台词允许被清空（用户说「这镜不要台词」），所以只要给了字段就收下
        if "dialogue" in item and item.get("dialogue") is not None:
            edit["dialogue"] = dialogue
        if item.get("duration") is not None:
            try:
                duration = float(item["duration"])
            except (TypeError, ValueError):
                duration = 0.0
            if duration:
                edit["duration"] = 10 if duration >= 8 else 5
        if len(edit) > 1:
            out.append(edit)
    return out[:24]


def plan_reply(plan: dict, shot_count: int) -> str:
    """铺完分镜的那句话自己写：小模型只会复述，而且铺完用户要的是「几个镜头、角色是谁」"""
    characters = plan.get("characters") or []
    shots = plan.get("shots") or []
    cast = (
        f"，{'、'.join(str(c.get('name')) for c in characters)} {len(characters)} 个角色"
        if characters else ""
    )
    short = f"（你要 {shot_count} 个，模型只给了 {len(shots)} 个）" if len(shots) < shot_count else ""
    # 和原文几乎对不上：宁可当场说出来，也别让人以为这就是自己要的剧本
    drift = ""
    if (plan.get("loyalty") or {}).get("weak"):
        drift = (
            f"\n⚠️ 不过这份分镜和你给的内容差得有点远（写成了《{plan.get('title')}》）。"
            "把剧情写得更具体些、或换成纯文本模型（如 glm-4-flash）再拆一次更稳。"
        )
    # 挂上参考图的角色不用再出定妆照，说一声，免得用户以为少生成了几张；
    # 反过来，图挂上了但模型读不了图（纯文本模型），也要说 —— 长相以用户的图为准，
    # 只是 desc 是按文字写的，别让人以为模型看过图。
    photos_info = plan.get("photos") or {}
    from_photo = len([c for c in characters if c.get("fromPhoto")])
    if from_photo:
        if photos_info.get("error"):
            photos = (
                f"你给的 {from_photo} 张图已经直接挂成定妆照了（没让 AI 再生成）。"
                f"不过这次模型读不了图：{photos_info['error']}。角色长相以你的图为准，文字描述是按剧情写的。"
            )
        else:
            photos = f"你给的 {from_photo} 张图已经直接挂成定妆照，不用再生成。"
    elif photos_info.get("error"):
        photos = (
            f"\n⚠️ 人物参考图这次没用上（{photos_info['error']}），角色长相是按文字写的 —— "
            "想照图定妆就换个支持读图的模型。"
        )
    else:
        photos = ""
    # 剧本是谁写的要交代一句：用户只写了一句题材，画布上却出来一整份剧本，
    # 不说清楚会以为模型又自己编故事了（这正是以前最招人烦的行为）
    script_info = plan.get("script") or {}
    if script_info.get("source") == "gpt":
        written = "\n这份剧本是本地 GPT 按你的题材写的（你只给了题材，没给整份剧本）。"
    elif script_info.get("note"):
        written = f"\n（{script_info['note']}）"
    else:
        written = ""
    return (
        f"《{plan.get('title')}》拆好了：{len(shots)} 个镜头{cast}，铺在画布上了{short}。"
        f"{written}{photos}{drift}"
    )


# ------------------------------------------------------------------ 主入口


async def chat_with_canvas(
    *,
    messages: Any = None,
    scene: Any = None,
    prefs: Any = None,
    photo_ids: list | None = None,
) -> dict:
    """聊一句。

    返回 `{reply, intent, plan?, edits?}`
      plan  —— 已拆好的分镜结构（前端直接铺节点）
      edits —— 要改的镜头（前端自己往节点上套）
    """
    deadline = now_ms() + CHAT_BUDGET_MS

    def left() -> int:
        return deadline - now_ms()

    history = [
        {"role": m["role"], "content": _text(m.get("content"), 4000)}
        for m in (messages if isinstance(messages, list) else [])
        if isinstance(m, dict)
        and m.get("role") in ("user", "assistant")
        and str(m.get("content") or "").strip()
    ][-MAX_TURNS:]

    last_user = next((m for m in reversed(history) if m["role"] == "user"), None)
    if not last_user:
        raise ValueError("说点什么吧")

    shots = scene.get("shots") if isinstance(scene, dict) and isinstance(scene.get("shots"), list) else []

    # 明确说要跑：不占用模型，直接回。
    if RUN_RE.match(last_user["content"].strip()):
        if not shots:
            return {"reply": "画布上还没有分镜，先把剧本发我，我拆好再开跑。", "intent": "chat"}
        todo = len([s for s in shots if not s.get("hasImage")])
        if not todo:
            return {
                "reply": "这些镜头都已经出好图了。想重画哪一镜，点开那个节点用面板里的重新生成。",
                "intent": "chat",
            }
        return {
            "reply": f"开跑，一共 {todo} 个还没出图的镜头。按「定妆照 → 镜头图 → 镜头视频」一波一波来，出完一批再下一批。",
            "intent": "run",
        }

    shot_count_of = lambda raw: max(2, min(12, round(float(raw or 6))))  # noqa: E731

    # 「把它拆成分镜」这种指代句：原文在前一轮，本地直接铺。
    # 交给模型只会多两次失败：它会把 prompt 里的占位符「要拆分镜的原文」抄进 script，
    # 还会把 JSON 包进 markdown 代码块导致解析失败（实测 C 场景整段返回了原文）。
    if is_plan_directive(last_user["content"]):
        script = find_earlier_script(history, last_user)
        if not script:
            return {"reply": "把要拆的剧本发我，或者再说清楚一点（我现在还不知道拆什么）。", "intent": "chat"}
        shot_count = shot_count_of((prefs or {}).get("shotCount"))
        plan = await direct_storyboard(
            script=script,
            shot_count=shot_count,
            style=(prefs or {}).get("style"),
            want_video=(prefs or {}).get("wantVideo", True) is not False,
            photo_ids=photo_ids,
            budget_ms=left(),
        )
        return {"reply": plan_reply(plan, shot_count), "intent": "plan", "plan": plan}

    # 用户直接把一段剧本贴进来：不用先问模型「这是什么意图」。
    #
    # 以前这类输入要走一次分类调用（实测 30~90 秒，偶尔 5xx 还要重试两次），
    # 把 280 秒的总预算吃掉一大半，后面真正拆镜时预算不够就被掐成
    # 「模型一直没有响应（超过 60 秒）」。剧本这种输入本地就能认出来：
    # 长、有事、多个分句 —— 直接走编导，把整段预算都留给它。
    # 引擎切成本地豆包时也走这条：豆包不需要分类调用（它一个来回就能既判意图又拆镜），
    # 但贴剧本是最确定的情况，直接进编导最省时间。
    if is_script_like(last_user["content"]) and not mentioned_shots(last_user["content"]):
        shot_count = shot_count_of((prefs or {}).get("shotCount"))
        plan = await direct_storyboard(
            script=last_user["content"].strip(),
            shot_count=shot_count,
            style=(prefs or {}).get("style"),
            want_video=(prefs or {}).get("wantVideo", True) is not False,
            photo_ids=photo_ids,
            budget_ms=left(),
        )
        return {"reply": plan_reply(plan, shot_count), "intent": "plan", "plan": plan}

    # 分类这一步也走豆包：用同一套 prompt，让它判意图 + 回一句话。
    # 好处是整条链路完全白嫖，而且豆包守 JSON 比免费对话模型强；
    # 代价是它会先点「新对话」，把聊天上下文清掉 —— 但这一步的 prompt 里本来就
    # 带着最近几轮历史（build_prompt 塞进去的），所以不依赖豆包自己的会话。
    prompt = build_prompt(messages=history, scene=scene, prefs=prefs)
    timeout_ms = max(30000, min(CLASSIFY_MAX_MS, left() - 180000))
    if director_engine() == "doubao":
        answer = await ask_doubao(prompt=prompt, timeout_ms=timeout_ms)
    else:
        # 分类调用不许吃掉全部预算：后面可能还要拆一遍分镜。
        # 至少留 200 秒给编导链路，所以这里取 min(90 秒, 剩余-180 秒)。
        # 90 秒而不是 60：实测 glm-4.5-flash 带 thinking 时这条也要 30~90 秒，
        # 卡 60 秒会在网络正常的情况下直接误报「超过 60 秒」。
        answer = await chat_script(prompt=prompt, temperature=0.5, timeout=timeout_ms)
    parsed = extract_json(answer)

    # 模型没守格式：直接把原文当回复，别把错误甩给用户 —— 聊天窗口里看到一句人话总比看到报错好
    if not parsed:
        return {"reply": _text(answer, 2000) or "我没太看懂，能再说一遍吗？", "intent": "chat"}

    intent = str(parsed.get("intent") or "").strip().lower() if isinstance(parsed, dict) else ""
    if intent not in INTENTS:
        intent = guess_intent(answer, last_user["content"])

    named = mentioned_shots(last_user["content"])
    # 画布上已经有分镜，用户又点名了「第 N 镜」——这就是改分镜，不可能是重新铺一份。
    # 小模型看到「雨夜」这种画面词就往 plan 上靠，不拦的话会在旁边再铺一整套分镜。
    if shots and named and len(last_user["content"]) < 400 and intent == "plan":
        intent = "revise"

    reply = trim_echo(_text(parsed.get("reply") if isinstance(parsed, dict) else None, 2000),
                      last_user["content"]) or "好。"

    if intent == "plan":
        # 剧本用**用户的原话**，模型给的那份只当「它抄的是哪一条」的线索（见 pick_script）
        script = pick_script(parsed.get("script") if isinstance(parsed, dict) else None,
                             history, last_user)
        try:
            shot_count = max(2, min(12, round(float(parsed.get("shotCount")))))
        except (TypeError, ValueError):
            shot_count = shot_count_of((prefs or {}).get("shotCount"))
        plan = await direct_storyboard(
            script=script,
            shot_count=shot_count,
            style=(prefs or {}).get("style"),
            want_video=(prefs or {}).get("wantVideo", True) is not False,
            photo_ids=photo_ids,
            budget_ms=left(),
        )
        return {"reply": plan_reply(plan, shot_count), "intent": intent, "plan": plan}

    if intent == "revise":
        edits = sanitize_edits(parsed.get("edits") if isinstance(parsed, dict) else None)
        # 小模型会把没点名的镜头一起改（历史里提过的也照改一遍）。
        # 用户点名了第几镜，就只认那几镜 —— 这一步比 prompt 里的叮嘱管用。
        if named:
            kept = [e for e in edits if e.get("shotIndex") in named]
            if kept:
                edits = kept
        # 说了要改却一条都没给：退回聊天语气，别让用户以为改好了
        if not edits:
            return {"reply": "我听出来你想改，但没拿到具体改法 —— 说下第几镜、改成什么？", "intent": "chat"}
        return {"reply": describe_edits(edits), "intent": intent, "edits": edits}

    return {"reply": reply, "intent": intent}


__all__ = [
    "CHAT_BUDGET_MS",
    "MAX_TURNS",
    "chat_with_canvas",
    "containment",
    "describe_edits",
    "describe_scene",
    "find_earlier_script",
    "guess_intent",
    "is_plan_directive",
    "mentioned_shots",
    "pick_script",
    "plan_reply",
    "sanitize_edits",
    "trim_echo",
]
