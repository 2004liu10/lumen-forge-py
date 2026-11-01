"""剧本引擎 · 本地 GPT 写剧本 —— 逐函数复刻 `server/script-writer.js`。

## 分工（照抄原版注释）

剧本（这一层）—— 本地 GPT（Codex CLI）把一句话题材写成一份能拍的剧本
分镜（下一层）—— 本地豆包把剧本拆成 JSON 分镜与提示词

**为什么把「写剧本」和「拆分镜」分成两层**：实测豆包拆分镜很强（守 JSON、地点不漏、
不自己编故事），但它**写故事不行** —— 给一句题材它会写得很平。GPT 反过来：故事写得
有层次，可它守 JSON 格式容易跑偏。各干各擅长的那一半，中间用「人话剧本」交接，
比让一个模型一次干完更稳。

交接物就是一份普通剧本（剧名 / 人物 / 分场 / 台词），不是 JSON —— 因为下游的
`direct_storyboard` 本来就能吃「一段剧本」，并且它认得出这是剧本（`is_script_like`
按「几件事」判），会走「忠实改编」那条路，不会自由发挥。
"""

from __future__ import annotations

from .codex_cli import ask_codex, find_codex_exe, probe_codex
from .store import store

#: 本地 GPT 写剧本的默认上限（与原版 `writeScriptWithGpt` 的 180000 一致）
DEFAULT_TIMEOUT_MS = 180000


def script_engine() -> str:
    """剧本引擎：'' = 不额外写剧本（用户给什么就拆什么）；'codex' = 本地 GPT 先写一版剧本"""
    raw = str((store.story_model or {}).get("scriptEngine") or "").strip().lower()
    return "codex" if raw == "codex" else ""


async def script_engine_info() -> dict:
    """界面上「检测」按钮用"""
    engine = script_engine()
    probe = await probe_codex()
    return {
        "engine": engine,
        "available": bool(probe.get("ok")),
        "exe": probe.get("exe") or find_codex_exe(),
        "version": probe.get("version") or "",
        "reason": probe.get("reason") or "",
    }


def build_script_prompt(*, idea: str, shot_count: int, style: str) -> str:
    """让 GPT 把一句话题材写成一份剧本（纯文字，不是 JSON）"""
    return "\n".join(
        [
            "你是一个短剧编剧。请把用户给的题材写成一集可以直接拍的短剧剧本。",
            "",
            "输出格式（只要正文，不要解释、不要 markdown 代码块、不要任何前后缀）：",
            "剧名：xxx",
            "人物：",
            "· 角色A：性别，年龄段，发型发色，五官特征，服装，气质（40~70 字）",
            "· 角色B：同上",
            "1. 地点：画面里发生什么 —— 谁在做什么、什么动作、什么表情、周围有什么（60~100 字）",
            "   台词：这个镜头里说的话（没有就写「无」）",
            "2. 地点：……",
            "   ……",
            "",
            f"一共写 {shot_count} 个镜头，按剧情顺序。",
            "",
            "要求：",
            "- 剧情要有起承转合，镜头之间连贯，前一个镜头的结尾要能接上后一个镜头的开头。",
            "- 每个镜头只写「一个能定格成一张图的瞬间」，不要在一个镜头里写一长串连续动作。",
            "- 每个镜头都要写清楚「在哪」，而且同一场戏的连续几个镜头用同一个地点 ——",
            "  后面出图要靠这个地点保持背景一致，地点一跳，画面就跳。",
            "- 人物描述要具体、可画：「单眼皮」「鼻梁左侧有一颗小痣」「灰色连帽卫衣」，",
            "  不要写「很漂亮」「有气质」这种画不出来的话。",
            "- 不要写真实人名、品牌名、地名。不要出现暴力、色情、政治内容。",
            "- 台词短，一句 15 字以内，像人说的话。",
            "",
            f"全片统一风格：{style}。（风格只在这里说一次，不要在正文里反复描述画风。）",
            "",
            f"用户给的题材是：「{idea}」",
        ]
    )


async def write_script_with_gpt(
    *,
    idea: str,
    shot_count: int = 6,
    style: str = "",
    timeout_ms: int | None = None,
) -> dict:
    """用本地 GPT 写一版剧本。返回 `{script, engine}`。

    失败就抛错，由上层决定是「退回直接拆题材」还是把错误抛给用户。
    """
    content = str(idea or "").strip()
    if len(content) < 4:
        raise ValueError("先写一句题材（至少 4 个字）")

    answer = await ask_codex(
        prompt=build_script_prompt(idea=content, shot_count=shot_count, style=style),
        timeout_ms=timeout_ms or DEFAULT_TIMEOUT_MS,
    )

    script = str(answer or "")
    # 模型偶尔会自己套一层 markdown 围栏，剪掉
    if script.lstrip().startswith("```"):
        script = script.lstrip()
        # 去掉开头的 ```lang
        nl = script.find("\n")
        script = script[nl + 1 :] if nl >= 0 else script[3:]
    if script.rstrip().endswith("```"):
        script = script.rstrip()[:-3]
    script = script.strip()
    if len(script) < 40:
        raise ValueError("本地 GPT 写的剧本太短，换个说法再试")

    return {"script": script, "engine": "codex"}
