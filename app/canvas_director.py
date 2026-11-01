"""节点画布 · AI 编导 —— 逐函数复刻 `server/canvas-director.js`。

用户贴一段剧本（也可以只写一句题材），这里把它拆成能直接铺到画布上的分镜：
  角色 → 镜头 → 视频

**只出文字结构，不建任务、不落库**。画布是用户自己的东西：铺成节点之后每一个
提示词、模型、连线都能手改，所以结构交给前端，生成仍旧走 `/api/image-tasks`、
`/api/video-tasks`。

为什么不复用 `storyboard.py` 那套：那是「小剧场」模块的数据模型（剧本、角色、
任务 id 全落在 db.storyboards 里），和画布是两种东西 —— 硬接会让同一份分镜
在两处各存一份，改一边另一边不知道。
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Callable

from .providers.doubao_bridge import ask_doubao
from .script_writer import script_engine, write_script_with_gpt
from .store import store
from .storyboard import chat_script, collect_photo_refs, extract_json, story_model_snapshot

# ------------------------------------------------------------------ 常量

#: 画风候选：给「文生图 + 图生视频」用得上的几种，前端也可以自己填
DIRECTOR_STYLES: list[dict[str, str]] = [
    {"id": "cinema", "label": "电影写实", "prompt": "电影感写实风格，35mm 胶片质感，自然光，浅景深，细节丰富"},
    {"id": "anime", "label": "日式动画", "prompt": "日式 2D 动画风格，干净线条，明亮色彩，赛璐璐上色"},
    {"id": "illust", "label": "扁平插画", "prompt": "扁平插画风格，柔和配色，简洁构图，轻微噪点质感"},
    {"id": "3d", "label": "3D 渲染", "prompt": "3D 渲染风格，柔和布光，卡通化比例，细腻材质"},
    {"id": "guofeng", "label": "国风水墨", "prompt": "国风水墨插画风格，留白构图，宣纸质感，淡彩"},
    {"id": "cyber", "label": "赛博霓虹", "prompt": "赛博朋克风格，霓虹光效，湿润反光，高对比冷色调"},
]

DEFAULT_DIRECTOR_STYLE = DIRECTOR_STYLES[0]["prompt"]

MIN_SHOTS = 2
MAX_SHOTS = 12
DEFAULT_SHOTS = 6

#: 整条编导链路的总时限（含补镜、重拆所有轮次）。
#: 必须比前端 `/api/canvas/chat` 的 300 秒超时短一截，否则前端先断，
#: 用户看到的是「signal timed out」而不是「分镜出来得慢」。
#: 实测一次 6 镜的完整编导约 225 秒，这里给 270 秒是留了重试余量。
DIRECTOR_BUDGET_MS = 270000

#: 补镜、跑题重拆只在剩余预算够跑一轮时才做：不够就别硬塞，先把手上这版交出去
TOPUP_MIN_MS = 75000
RETRY_MIN_MS = 90000

#: 造型设定最多几个：再多定妆照也是一张一张出，等得久
MAX_CHARACTERS = 4

#: 地点最多几个：一场戏通常就一两个地方，再多就说明剧本被拆散了
MAX_SCENES = 6

#: 常见地点词。免费小模型（glm-4v-flash）写 scene 经常只写「谁在做什么」，
#: 地点一句不写 —— 出图时模型没地方可依，就自己编一个房间，于是同一场戏
#: 一镜在厨房、下一镜在客厅。这个表用来从画面文字里认出「这镜在哪」，
#: 认不出来就继承上一镜的地点（同一场戏默认不换地方）。
PLACE_WORDS: list[str] = [
    "厨房", "客厅", "卧室", "书房", "餐厅", "阳台", "卫生间", "浴室", "玄关", "走廊", "楼梯",
    "院子", "庭院", "花园", "屋顶", "天台", "地下室", "车库", "电梯", "办公室", "会议室",
    "教室", "图书馆", "医院", "病房", "咖啡店", "咖啡厅", "酒吧", "书店", "超市",
    "商场", "街道", "街头", "马路", "巷子", "小巷", "广场", "公园", "河边", "海边", "湖边",
    "山上", "山顶", "森林", "树林", "田野", "草原", "沙漠", "雪地", "地铁站", "车站", "机场",
    "火车站", "码头", "船上", "车内", "车里", "操场", "球场", "泳池", "健身房",
    "宫殿", "大殿", "闺房", "客栈", "酒楼", "茶馆", "庙", "寺", "牢房", "营帐",
    "床前", "床边", "窗前", "窗边", "门口", "门前", "桌边", "灶台", "灶前",
]

#: 模型没给 motion 时的兜底运镜：不同镜头给不同的一句，别让整片视频的运镜一模一样
CAMERA_MOVES: list[str] = [
    "镜头缓慢推近，人物自然呼吸，画面有细微动态",
    "镜头轻微横移，人物动作连贯，背景有轻微流动感",
    "镜头固定，人物表情和动作自然变化，发丝和衣角轻轻动",
    "镜头缓慢拉远，环境细节依次展开，人物保持动作",
]

#: 景别：只认这五档。模型爱写「中近景」「大特写片」这种混档，按里面出现的档位归
SHOT_SIZES = ["特写", "近景", "中景", "全景", "远景"]

#: 机位角度：只认这几档，认不出来就平视（默认档，写进提示词不会误导）
SHOT_ANGLES = ["平视", "低角度", "高角度", "俯拍", "仰拍", "过肩"]

#: 重合率低于这个值就算「跑偏」，重试一次。
#:
#: 实测标定（同一段剧本 + glm-4v-flash）：
#:   真跑偏（《迷雾中的舞者》《雨夜奇遇》）→ 0.026
#:   忠实但换了角色名、把「做饭」写成「做早餐」 → 0.077 ~ 0.128
#: 所以阈值卡在 0.05：只抓「几乎一个字都不沾」的，别把正常改写误判成跑题。
LOYALTY_MIN = 0.05


# ------------------------------------------------------------------ 小工具


def _text(value: Any, max_len: int) -> str:
    """`text(v, max)` —— JS 的 `String(v ?? '').trim().slice(0, max)`"""
    if value is None or isinstance(value, (dict, list)):
        raw = "" if value is None else json.dumps(value, ensure_ascii=False)
    else:
        raw = str(value)
    return raw.strip()[:max_len]


def _list(value: Any) -> list:
    """JS 的 `Array.isArray(x) ? x : []`"""
    return value if isinstance(value, list) else []


def now_ms() -> int:
    return int(time.time() * 1000)


def _punct_strip(value: str) -> str:
    r"""`replace(/[\s\p{P}]+/gu, '')` —— 去掉所有空白与标点（保留汉字/字母/数字）"""
    return re.sub(r"[\s\W_]+", "", value, flags=re.UNICODE)


# ------------------------------------------------------------------ 地点识别


def detect_place(text_value: Any) -> str:
    """从一段画面文字里认出地点（取最先出现、最具体的那个词）"""
    body = "" if text_value is None else str(text_value)
    best = ""
    best_at = len(body) + 1
    for word in PLACE_WORDS:
        at = body.find(word)
        if at < 0:
            continue
        # 位置靠前的优先；同一位置取更长的（「厨房门口」比「厨房」具体）
        if at < best_at or (at == best_at and len(word) > len(best)):
            best = word
            best_at = at
    return best


# ------------------------------------------------------------------ 引擎


def director_engine() -> str:
    """编导引擎：'api' = 配置好的 OpenAI 兼容模型；'doubao' = 本地豆包客户端（白嫖）。

    存在 `store.story_model.engine` 里，跟剧本模型配置放一起，界面上就是一个开关。
    """
    raw = str((store.story_model or {}).get("engine") or "").strip().lower()
    return "doubao" if raw == "doubao" else "api"


def doubao_video_busy() -> bool:
    """豆包窗口是共享的：编导一问一答会点「新对话」，把窗口里正在跑的那个视频对话切走 ——
    视频任务再也等不到自己的片子，只能超时失败。所以有豆包视频任务在推进时，
    编导这条路先让一让，把话说清楚让用户等一会儿或换云端模型。
    """
    active = {"submitting", "queued", "polling", "downloading"}
    for task in store.video_tasks:
        if task.get("status") not in active:
            continue
        model = next((m for m in store.video_models if m.get("id") == task.get("modelId")), None)
        if (model or {}).get("provider") == "doubao":
            return True
    return False


def director_info() -> dict:
    """给界面用的静态信息：画风、镜头数范围、剧本模型有没有配好"""
    model = story_model_snapshot()
    name = str(model.get("model") or "")
    engine = director_engine()
    # 写剧本的那一层（本地 GPT）也跟着报出去：界面要能显示「剧本由谁写」，
    # 检测按钮也要能告诉用户「装了但没登录」这种中间状态
    s_engine = script_engine()
    return {
        "styles": DIRECTOR_STYLES,
        "defaultStyle": DEFAULT_DIRECTOR_STYLE,
        "minShots": MIN_SHOTS,
        "maxShots": MAX_SHOTS,
        "defaultShots": DEFAULT_SHOTS,
        # 用豆包当引擎时不需要接口地址和 Key，只要有窗口就行
        "engine": engine,
        "hasModel": True if engine == "doubao" else bool(
            model.get("baseUrl") and model.get("model") and model.get("hasKey")
        ),
        "modelName": "本地豆包" if engine == "doubao" else str(model.get("model") or ""),
        # 多模态模型（glm-4v-flash、qwen-vl…）写剧本经常不听指令、自己编故事，
        # 免费用户图省事常拿它顶上，界面上得提醒一句换纯文本模型
        "modelIsVision": False if engine == "doubao" else bool(
            re.search(r"(^|[-_.])(v|vl|4v)([-_.]|$)|vision|4v-", name, re.I)
        ),
        # 写剧本这一层：'' = 不写（用户给什么拆什么），'codex' = 本地 GPT 先写一版
        "scriptEngine": s_engine,
        "scriptEngineName": "本地 GPT（Codex）" if s_engine == "codex" else "不写（直接拆你给的内容）",
    }


# ------------------------------------------------------------------ 剧本判定


def count_beats(value: Any) -> int:
    """数「这件事」有几件：按分号 / 箭头 / 换行 / 句号切开，长度 ≥2 的算一件"""
    body = "" if value is None else str(value)
    parts = re.split(r"[；;→\n]+|。+", body)
    return len([p for p in parts if len(p.strip()) >= 2])


def is_script_like(value: Any) -> bool:
    """这段文字是不是「一整个剧本」而不是一句主题。

    以前只看字数（≥120 字才算剧本），但用户经常写的是**镜头清单**：
    「起床伸懒腰；拉开窗帘；下楼看到她在做饭；上去搂腰；她脸红戳了一下」——
    70 个字，五个动作，字数不够 120，被判成「一句话题材」，模型就直接自由发挥，
    拆出来的是一整个不相干的故事（实测：厨房拥抱 → 雨夜伞下）。按「几件事」判才准。
    """
    body = "" if value is None else str(value).strip()
    return len(body) >= 120 or count_beats(body) >= 3


# ------------------------------------------------------------------ 提示词


def build_prompt(
    *,
    script: Any,
    shot_count: int,
    style: str,
    want_video: bool,
    photo_count: int = 0,
    photos_attached: bool = True,
) -> str:
    """把「一段剧本 / 一句题材」包成编导提示词。

    `None` = 这一行不需要（只用来吃掉条件分支），空串是**故意的空行**，不能一起过滤掉。
    """
    is_full_script = is_script_like(script)
    lines: list[str | None] = [
        "你是一个短视频导演。把用户给的内容拆成分镜，供「文生图 / 图生视频」模型直接使用。",
        "",
    ]
    if photo_count:
        if photos_attached:
            lines.append(
                f"用户还附了 {photo_count} 张人物参考图（见输入末尾）。"
                f"**角色正好 {photo_count} 个，一张图对应一个角色，按图的顺序排**；"
                "每个角色的 desc 要照着图里那个人写：发型发色、五官、肤色、穿着、体型气质都要对得上，"
                "后面全片就靠这段描述和这张图保持长相一致。"
            )
        else:
            # 纯文本模型接不了图：图仍然会挂成定妆照（长相以用户的图为准），
            # 但 desc 只能按用户给的文字写，别再要求它「照着图写」
            lines.append(
                f"用户给了 {photo_count} 张人物参考图，但**这次没发给模型**（当前模型不读图）。"
                f"**角色正好 {photo_count} 个，按顺序对应那几张图**；"
                "每个角色的 desc 按用户给的文字写，写得具体、可画就行 —— 长相最后以用户的参考图为准，"
                "不要编图里的细节，也不要写「很漂亮」这种画不出来的话。"
            )
    else:
        lines.append(None)

    lines += [
        "",
        "用户给的是一段剧本，请**忠实改编**：人物、台词、剧情顺序都要保留，不要另编一个故事。"
        if is_full_script
        else "用户给的是一句题材或简短描述，请围绕它编一个有起承转合的小故事。",
        "",
        "只输出 JSON，不要任何解释、不要 markdown 代码块。JSON 格式：",
        '{"title":"剧名","setting":"全片统一设定","places":["地点名"],"characters":[{"name":"角色名","desc":"外貌与服装描述"}],"shots":[{"place":"地点名","scene":"画面描述","shotSize":"景别","angle":"机位","light":"光线","motion":"镜头运动","endState":"这一镜结束时的画面","dialogue":"台词","duration":10,"characters":["角色名"]}]}',
        "",
        "下面是一个**只用来示意长度和写法**的例子（内容不要照抄）：",
        '{"title":"末班车","setting":"深夜的空荡地铁站台，冷白荧光灯管，清冷偏青的色调，电影感浅景深，35mm 胶片质感，细腻写实，低饱和，8k 高清","places":["地铁站台"],"characters":[{"name":"阿岚","desc":"二十七岁的女性，齐耳短发染成亚麻色，单眼皮，鼻梁左侧有一颗小痣，穿灰色连帽卫衣和洗旧的牛仔裤，神情疲惫，肩膀微微含着"}],',
        ' "shots":[{"place":"地铁站台","scene":"阿岚站在深夜的地铁站台黄线边，双手抱着一只纸箱贴在胸前，低头看箱盖，眉头轻轻皱着，人占画面中部偏左，半身入画；身后荧光灯管忽明忽暗，远处立柱上贴着褪色的广告，脚边放着一只旧帆布包","shotSize":"中景","angle":"平视","light":"头顶冷白荧光，灯管闪动，边缘偏暗","motion":"阿岚先把纸箱换到左手，抬手拨开额前的碎发，镜头从站台尽头缓慢推近到中景","endState":"阿岚抬起头看向隧道尽头，纸箱仍抱在怀里，发丝被进站的风吹起","dialogue":"","duration":10,"characters":["阿岚"]}]}',
        "",
        "各字段要求：",
        "- title：剧名，12 字以内。",
        "- setting：全片统一的画面设定，**40~80 字，一段话**，全片每一镜都会共用它，所以写的是「这个片子长什么样」而不是剧情。",
        "  按顺序写：空间类型与整体环境 → 时间与天气 → 主光源与光线质感 → 色调与饱和度 → 质感与画质。",
        "  例：「现代都市温馨居民宅，清晨，柔和自然侧光，暖调低饱和，电影感浅景深，细腻写实，35mm 胶片质感，8k 高清」。",
        "  **不要写具体某个人、某个动作、某一镜的道具**（那是 scene 的事），也不要写画风名 —— 画风已经单独定了。",
        f"- places：这个故事一共发生在几个地方，1~{MAX_SCENES} 个，每个写成 2~8 个字的**具体地点名**"
        "（「厨房」「卧室」「小区花园」「公司茶水间」），不要写「室内」「室外」「某处」这种没信息量的。",
        "  · 一个地方可以连着好几镜，不用一镜换一个地方。",
    ]
    if photo_count:
        lines.append(f"- characters：正好 {photo_count} 个（一张参考图一个，顺序一致）。")
    else:
        lines.append(f"- characters：1~{MAX_CHARACTERS} 个角色，只列**真正出现在画面里**的人。")
    lines += [
        "  · name：角色名，2~6 个字，用中文名，不要英文名或外国译名。",
        "  · desc：写清「性别、年龄段、发型发色、五官特征、服装、气质」，**50~80 字**。",
        "    这段描述会被拿去生成角色定妆照，也是全片人物长相一致的唯一依据，",
        "    所以必须具体、可画 —— 不要写「很漂亮」「有气质」这种画不出来的话，",
        "    要写「单眼皮」「鼻梁左侧有一颗小痣」「灰色连帽卫衣」这种画得出来的特征。",
        f"- shots：正好 {shot_count} 个镜头，按剧情顺序。**每一镜必须是能定格成一张图的一瞬间**，不是一段连续动作。",
    ]
    # 小模型（尤其视觉小模型）特别容易把「角色数」当成「镜头数」：
    # 给了 2 张人物图，它就只写 2 镜。这里把两件事拆开说清楚。
    if photo_count:
        lines.append(
            f"  · 注意：{shot_count} 是**镜头数**，跟上面「角色正好 {photo_count} 个」没关系 —— "
            f"就算只有 {photo_count} 个角色，也要拍 {shot_count} 个不同机位/不同瞬间的镜头。"
        )
    else:
        lines.append(None)
    lines += [
        f"  · **输出前先数一遍 shots 数组的长度，必须正好 {shot_count} 个**：少了就接着往下补成新镜头，"
        "不要提前收尾；用户列了几件事、几个动作，就一件事一镜，别把两三件事压进同一镜。",
        "  · place：这一镜发生在哪个地方，**必须从上头 places 里挑一个原样填**，不要自己新编。",
        "    同一场戏连着几镜就都填同一个地点 —— 这是防止「上一镜在厨房、下一镜莫名跑到客厅」的关键字段，**不要留空**。",
        "  · scene：这一镜定格的那幅画面，**60~100 字**，按「谁在做什么 → 在哪、周围有什么 → 人在画面里占多大」的顺序写：",
        "    1. 主体：谁、什么姿态、什么表情。有人物参考图的角色**不写长相**（年龄发型五官肤色一概不写），只写姿态和表情；没有参考图的才写长相。",
        "       表情要具体：「嘴角压着一点笑」「眉头皱着、嘴唇抿紧」，不要只写「开心」「难过」。",
        "    2. 动作：一个**正在发生**的动作，用进行态（「正抬手推开窗户」「低头看着箱盖」）。",
        "       不要写「先…然后…」「站起来走向…」这种一段时间里发生的事 —— 图像模型只会画出其中一帧，而且经常画错那一帧。",
        "    3. 环境：地点、时间、天气，再加 **2~3 个具体道具或环境细节**（「灶台上冒着热气的不锈钢锅」「脚边一只旧帆布包」「窗台上落了一层灰」）。",
        "       道具要能说明这个人在干什么、这个地方是谁的，不要写「一些家具」「各种摆设」这种空话。",
        "       **scene 开头或动作后面必须把这个地点写出来**（「…在厨房的灶台前切着菜」），不能只写人物动作 ——",
        "       只写动作的话出图时模型会自己编一个房间，同一场戏的两镜就会一个在厨房一个在客厅。",
        "    4. 占比：写清主体在画面里的大小和位置（「人占画面中部，半身入画」「人物偏右，背景大面积留白」）。",
        "    只写看得见的东西。不要写画风，不要用「镜头」「画面」开头，不要写心理活动（「她想起…」），不要写比喻。",
        "    不要照抄剧本原句：剧本写「她坐下」，要改写成「她正坐到窗边的木椅上，双手交叠放在膝头，肩膀松下来」。",
        "  · shotSize：这一镜的景别，只能填「特写 / 近景 / 中景 / 全景 / 远景」之一，**不要留空**。",
        "    交代环境用全景，对话和情绪用近景，道具细节用特写 —— 全片不要都是同一种景别。",
        "  · angle：机位角度，只能填「平视 / 低角度 / 高角度 / 俯拍 / 仰拍 / 过肩」之一。",
        "    情绪压迫用低角度，交代环境用高角度，对话用平视或过肩，全片不要一直平视。",
        "  · light：这一镜的光线，**10~20 字**，写清光源方向和质感（「窗外柔和侧光，皮肤有细腻高光」「傍晚暖色逆光，轮廓发亮」）。",
        "    同一场戏里光线要一致，只在这个镜头换场或换时间时才变。",
        "  · endState：这一镜的动作做完之后，画面**停在哪** —— 20~45 字，同样写成一个能定格的画面。",
        "    它同时是两样东西：下一镜 scene 的起点，以及这一段视频的最后一帧。",
        "    所以 endState 必须和下一镜的 scene 是同一个瞬间：同一套服装、同一个朝向、同一种光线。",
        "    例：scene 写「她正抬手推开窗户」，endState 就写「窗户推开一半，她探身向外，侧脸被风吹起几缕碎发」。",
        "    **endState 里出现的动作，motion 必须已经走到**：endState 写「坐直，抬手拢了一下头发」，",
        "    motion 就得写到「…完全坐直，抬手拢了一下头发」；只写「坐起来、揉揉脖子」的话，",
        "    模型会在揉脖子那儿就收尾，尾帧那张「整理头发」的图接不上。",
        "  · motion：这一镜的**运镜方式 —— 只写机位怎么动**，10~20 字。",
        "    例：「缓慢推近，轻微下移」「固定机位，轻微手持感」「从左侧缓慢横移跟拍」。",
        "    **不要把人物的动作写进来**（人物的动作已经写在 scene 和 endState 里，重复一遍只会让模型做两遍）。",
        "    全片不要每一镜都用同一个运镜；交代环境用推拉或横移，情绪停留用固定机位。",
        "  · dialogue：这个镜头里的台词，15 字以内；没有台词就填空字符串。",
        "  · duration：这个镜头多长，只能填 5 或 10（秒）。**默认都填 10**（成片默认横屏 10 秒一段），",
        "    只有动作特别短、节奏要快的镜头才填 5。",
        "  · characters：这个镜头画面里出现的角色名，必须是上面 characters 里出现过的名字；",
        "    一个角色都不出现的空镜就填空数组。**这个字段决定了出图时挂谁的参考图，不要漏填**。",
        "",
        "硬性要求：",
        "- 镜头之间要连贯：上一镜和下一镜是同一场戏的推进，不要 12 个互不相关的画面。",
        "- **地点不能乱跳**：除了剧情真的换地方（并且要在 scene 里写明「走到…」「推开门进了…」），",
        "  连着几镜都在同一个地方。人物的家就是一个地方，别一镜卧室一镜客厅一镜又卧室。",
        "- **首尾相接**：成片是「图 1 → 视频 → 图 2 → 视频 → 图 3 …」串起来的，一张图既当上一段视频的尾帧、",
        "  又当下一段视频的首帧。所以第 N 镜的 endState 就是第 N+1 镜 scene 的起点，",
        "  相邻镜头之间服装、发型、妆容、光线、人物朝向都要能对上；",
        "  人物在画面里的位置也不要跳（上一镜人在左边，下一镜别毫无交代地跑到右边）。",
        "- 最后一镜的 endState 写这一场戏的收尾画面（不需要再接下一镜）。",
        "- 不要出现真实人名、品牌名、地名。",
        "- 不要暴力、色情、政治相关内容。",
        f"- 全片统一画风：{style}。**不要把画风写进 scene**，前端会统一拼。",
        "- 每段视频只跑「本镜 scene → 本镜 endState」这一小段：动作要短、要收得住，别写成一段几分钟的连续剧情。"
        if want_video
        else None,
        "",
        "用户给的内容：",
        '"""',
        str(script).strip(),
        '"""',
    ]
    return "\n".join(line for line in lines if line is not None)


# ------------------------------------------------------------------ 字段清洗


def pick_shot_size(raw: Any, scene: Any) -> str:
    """画面里已经写了景别就以它为准：scene 才是最终要画的那句话，跟它打架没意义
    （模型常出现「scene 写中景、shotSize 填中近景」这种自相矛盾）
    """
    scene_text = "" if scene is None else str(scene)
    for size in SHOT_SIZES:
        if size in scene_text:
            return size
    value = _text(raw, 12)
    if value:
        for size in SHOT_SIZES:
            if size in value:
                return size
    # 两处都没写就留空，前端也就不会补 —— 瞎猜一档比不写更糟
    return ""


def pick_light(raw: Any) -> str:
    """光线：模型偶尔写一大段，砍短；「无 / 略」这种等于没给"""
    value = re.sub(r"^[，,。；;、]+", "", _text(raw, 40))
    if not value or re.match(r"^(无|略|暂无|不明确|自然光)$", value):
        return ""
    return value


def pick_angle(raw: Any) -> str:
    """机位角度：只认这几档，认不出来就平视（默认档，写进提示词不会误导）"""
    value = _text(raw, 12)
    for angle in SHOT_ANGLES:
        if angle in value:
            return angle
    return "平视"


def pick_end_state(raw: Any, next_shot: dict | None) -> str:
    """「这一镜结束时画面停在哪」。

    老分镜（升级上来的）没有这个字段，就退回下一镜的 scene ——
    反正成片本来就是拿下一镜的图当尾帧，两者说的是同一个瞬间。
    """
    value = _text(raw, 200)
    if value:
        return value
    nxt = _text((next_shot or {}).get("scene"), 200)
    return nxt[:80] if nxt else ""


def sanitize_plan(parsed: Any, *, shot_count: int, style: str, photos: list[dict]) -> dict:
    """把模型给的一堆镜头规整成能直接用的结构（免费的对话模型经常少字段、给超范围的值）。

    导出是为了能被冒烟直接验证字段清洗（不联网也能测）。
    """
    title = _text((parsed or {}).get("title"), 30) or "未命名分镜"

    # 全片统一设定：模型给的 setting 是「这个片子长什么样」的一段话，
    # 每一镜都共用。它和 style（画风下拉）不是一回事：style 是「电影感写实」这种风格名，
    # setting 是「现代都市居民宅 + 清晨 + 柔和侧光 + 暖调低饱和 + 胶片质感」这种具体基调。
    setting = _text((parsed or {}).get("setting"), 300)

    characters: list[dict] = []
    for c in _list((parsed or {}).get("characters"))[:MAX_CHARACTERS]:
        name = _text((c or {}).get("name"), 20)
        desc = _text((c or {}).get("desc"), 300)
        if name and desc:
            characters.append({"name": name, "desc": desc})
    characters = characters[:MAX_CHARACTERS]

    # 用户给了人物图：按顺序把图直接挂成这些角色的定妆照 ——
    # 这一步跳过「AI 出定妆照」，人物就是用户给的那个人，之后每个镜头都拿它当参考图。
    # 注意：模型读不了图（纯文本模型）时 photos 仍然留着，所以这里照样能挂上 ——
    # 用户要的就是「模型看不懂图没关系，把我的图直接放上画布」。
    for i, photo in enumerate(photos):
        if i >= len(characters):
            break
        ref = photo.get("ref") or {}
        characters[i]["refId"] = ref.get("id")
        characters[i]["refUrl"] = ref.get("localUrl") or ""
        characters[i]["fromPhoto"] = True

    # 模型列的角色比用户给的图少（小模型常把 3 张图写成 2 个角色）：多出来的图也得落到画布上，
    # 不能让用户传的图有一张没地方去。补出来的角色只用来挂图，不参与出图提示词的描述。
    for i in range(len(characters), len(photos)):
        base = f"角色{i + 1}"
        name = base
        n = 2
        while any(c.get("name") == name for c in characters):
            name = f"{base}-{n}"
            n += 1
        ref = photos[i].get("ref") or {}
        characters.append({
            "name": name,
            "desc": "按你给的参考图定妆",
            "refId": ref.get("id"),
            "refUrl": ref.get("localUrl") or "",
            "fromPhoto": True,
        })

    names = {c["name"] for c in characters}
    # 地点表：模型给的 places 只用来「约束镜头别乱填」，最终以每镜自己的 place 为准
    declared = [
        _text(p, 20) for p in _list((parsed or {}).get("places"))
    ]
    declared = [p for p in declared if p][:MAX_SCENES]

    raw_shots: list[dict] = []
    for index, s in enumerate(_list((parsed or {}).get("shots"))):
        if not isinstance(s, dict):
            continue
        scene = _text(s.get("scene"), 500)
        if not scene:
            continue
        # 视频模型基本只有 5 秒 / 10 秒两档。默认给 10 秒（这一版主用横屏 10 秒一段），
        # 只有模型明确写 5 才给 5；写 8、12 这种按就近归到两档上
        try:
            raw_duration = float(s.get("duration"))
        except (TypeError, ValueError):
            raw_duration = 0.0
        duration = 5 if (raw_duration > 0 and raw_duration < 8) else 10
        # 模型经常忘记填 characters（特别是免费小模型）。漏了就退回「镜头文字里提到谁就算谁」——
        # 这个字段决定出图挂谁的参考图，缺了人物就每张一个样。
        # 注意是「并集」不是二选一：模型填了但不全的情况更多（写了主角、漏了对面那个人），
        # 只认它填的那份，漏掉的角色就既拿不到参考图、名字也换不成图号，两张脸照样串。
        named = [
            _text(n, 20) for n in _list(s.get("characters"))
        ]
        named = [n for n in named if n in names]
        dialogue = _text(s.get("dialogue"), 100)
        spoken = f"{scene}{dialogue}"
        mentioned = [c["name"] for c in characters if c["name"] in spoken]
        # 模型填的排前面（它知道谁是主体），文字里提到的补在后面
        in_shot = list(dict.fromkeys([*named, *mentioned]))[:MAX_CHARACTERS]
        # 地点三路兜底：模型填的 place → 从 scene 文字里认 → 留给下面「继承上一镜」
        given_place = _text(s.get("place"), 20)
        raw_shots.append({
            "index": index + 1,
            "scene": scene,
            "place": given_place or detect_place(scene),
            # 景别 / 光线单拎出来：写进提示词时排在最合适的位置，
            # 混在 scene 里模型经常漏写，拼提示词的人也没法控制顺序
            "shotSize": pick_shot_size(s.get("shotSize"), scene),
            "angle": pick_angle(s.get("angle")),
            "light": pick_light(s.get("light")),
            # motion 缺了就按镜头序号轮换一条兜底运镜 —— 图生视频至少不会因为没提示词而失败，
            # 也不会整片都是同一句「镜头缓慢平移」
            "motion": _text(s.get("motion"), 200) or CAMERA_MOVES[index % len(CAMERA_MOVES)],
            "rawEndState": _text(s.get("endState"), 200),
            "dialogue": dialogue,
            "duration": duration,
            "characters": in_shot,
        })
    raw_shots = raw_shots[:shot_count]

    # 地点补齐：模型常常整片一个 place 都不填。这时不能让它空着 ——
    # 空着出图时模型自己编房间，同一场戏就会一镜厨房一镜客厅（用户实际踩到的坑）。
    # 规则：自己认得出就用自己认出的，认不出就继承上一镜；第一镜认不出就用 places 的第一个。
    carried = ""
    for shot in raw_shots:
        if not shot["place"]:
            shot["place"] = carried or (declared[0] if declared else "")
        carried = shot["place"] or carried

    # endState 要看着「下一镜」才填得准（没有就退回下一镜的 scene），所以等整列镜头都洗完再补
    shots: list[dict] = []
    for index, shot in enumerate(raw_shots):
        raw_end = shot.pop("rawEndState")
        nxt = raw_shots[index + 1] if index + 1 < len(raw_shots) else None
        shots.append({**shot, "endState": pick_end_state(raw_end, nxt)})

    # 全片地点清单：用镜头里实际出现的顺序去重，给前端做「这一场在哪」的展示与批量改
    places: list[str] = []
    for shot in shots:
        if shot["place"] and shot["place"] not in places:
            places.append(shot["place"])

    return {"title": title, "setting": setting, "style": style, "places": places,
            "characters": characters, "shots": shots}


def parse_shots_answer(answer: Any) -> list[dict]:
    """从模型的回答里抠出「一串镜头」：可能是 `{"shots":[…]}`，也可能直接是 `[…]`"""
    got = extract_json(answer)
    if isinstance(got, list):
        return got
    if isinstance(got, dict) and isinstance(got.get("shots"), list):
        return got["shots"]
    raw = re.sub(r"^```(?:json)?", "", str(answer or "").strip(), flags=re.I)
    raw = re.sub(r"```$", "", raw).strip()
    start = raw.find("[")
    end = raw.rfind("]")
    if start >= 0 and end > start:
        try:
            arr = json.loads(raw[start : end + 1])
        except (ValueError, TypeError):
            return []
        if isinstance(arr, list):
            return arr
    return []


# ------------------------------------------------------------------ 补镜


def _shots_of(value: Any) -> list[dict]:
    """`shotsOf(value)`：取镜头数组，并过滤掉没有 scene 的"""
    if isinstance(value, list):
        items = value
    elif isinstance(value, dict) and isinstance(value.get("shots"), list):
        items = value["shots"]
    else:
        return []
    return [s for s in items if isinstance(s, dict) and _text(s.get("scene"), 500)]


async def top_up_shots(
    *,
    ask: Callable,
    parsed: Any,
    want: int,
    left: Callable[[], int] | None = None,
) -> Any:
    """镜头数不够就接着补。

    实测两个模型都会少给：prompt 里明明写着「正好 6 个镜头」，glm-4v-flash 只给 2 个
    （它把「2 张人物图 / 2 个角色」当成了镜头数），glm-4.6 偶尔也提前收尾。
    以前只能靠那句「你要 6 个，模型只给了 2 个」如实告知，用户得自己再拆一遍。
    现在改成：已经写好的几镜留着，让它从最后一镜的收尾画面接着往下写缺的那几镜。

    最多补两轮 —— 补不出来就认了，不能让一份能用的分镜卡在这里。
    """
    current = parsed
    for _round in range(2):
        shots = _shots_of(current)
        if len(shots) >= want:
            break
        # 剩余预算不够再跑一轮补镜了：与其硬塞到超时，不如把手上这几镜交出去
        if left is not None and left() < TOPUP_MIN_MS:
            break
        missing = want - len(shots)
        last = shots[-1]
        names = [
            _text(c.get("name"), 20) for c in _list((current or {}).get("characters"))
        ]
        names = [n for n in names if n]

        def place_of(s: dict) -> str:
            return _text(s.get("place"), 20) or detect_place(s.get("scene"))

        # 补镜这一轮也要把地点带上：不然补出来的几镜又是一片没地点可依的画面，
        # 前后接上去就成了「同一个人忽而厨房忽而客厅」
        places: list[str] = []
        for s in shots:
            p = place_of(s)
            if p and p not in places:
                places.append(p)
        last_place = place_of(last) or (places[-1] if places else "")
        # 全片设定要一起带进补镜这一轮：不然补出来的几镜会换一套光线和色调，
        # 拼在同一片里就成了「前两镜暖光、后两镜冷白」
        setting = _text((current or {}).get("setting"), 300)

        parts = [
            "**这一轮只补镜头，不要重写、不要重复已经有的那几镜。**",
            f"上一份 JSON 里 shots 只有 {len(shots)} 个，还差 {missing} 个。已有的：",
            # 模型给的镜头没有 index 字段（那是清洗时补的），编号只能按数组顺序数
            "\n".join(
                f"第 {i + 1} 镜（{place_of(s) or '未注明地点'}）：{_text(s.get('scene'), 120)}"
                for i, s in enumerate(shots)
            ),
            f"第 {len(shots)} 镜收尾在原画面：{_text(last.get('endState'), 200) or _text(last.get('scene'), 120)}",
            "",
            f"全片统一设定（补的这几镜必须跟它一致）：{setting}" if setting else "",
            "",
            f"接着往下写第 {len(shots) + 1} 到第 {want} 镜，只输出这个 JSON（不要解释、不要代码块）：",
            '{"shots":[{"place":"…","scene":"…","shotSize":"…","angle":"…","light":"…","motion":"…","endState":"…","dialogue":"","duration":10,"characters":[]}]}',
            f"- shots 里**正好 {missing} 个元素**，字段要求跟上面那份总则完全一样（scene 60~100 字、能定格、不写长相、不写画风）。",
        ]
        if last_place:
            parts.append(
                f"- 地点：上一镜在「{last_place}」，这一镜**默认还在这里**，place 就填「{last_place}」；"
                "真的要走出去，必须先在 scene 里写明「推开…的门」「走到…」，再换 place。"
            )
        else:
            parts.append("- 地点：每镜都要填 place，同一场戏连着几镜填同一个地方。")
        parts.append(
            f"- 必须接着第 {len(shots)} 镜往下讲：同一套服装、同一个朝向、同一种光线，别跳到别的场景。"
        )
        if names:
            parts.append(
                f"- characters 只能填这些名字：{'、'.join(names)}；一个角色都没有的空镜填空数组。"
            )
        else:
            parts.append("- characters 填画面里出现的人名，空镜填空数组。")

        extra = "\n".join(parts)

        added: list[dict] = []
        try:
            added = _shots_of(parse_shots_answer(await ask(extra, 0.55)))
        except Exception:  # noqa: BLE001 - 补镜这一路挂了不影响主流程，手上这几镜照常用
            break
        if not added:
            break
        # 小模型收到「接着往下写」经常干脆整份重写一遍：它一次给的够数，就说明
        # 这是一套完整的新分镜 —— 直接换掉，别再往旧的两镜后面接（会重复前三镜）
        if len(added) >= want:
            current = {**(current if isinstance(current, dict) else {}), "shots": added[:want]}
            break
        # 没重写就往下接，但要把「一字不差抄了前面某镜」的剔掉，否则同一幅画面会出现两次
        seen = {_text(s.get("scene"), 12) for s in shots}
        fresh = [s for s in added if _text(s.get("scene"), 12) not in seen]
        if not fresh:
            break
        # 模型多给也照收，但要截到想要的镜数 —— 下游按「正好 N 镜」铺节点
        current = {
            **(current if isinstance(current, dict) else {}),
            "shots": [*shots, *fresh][:want],
        }
    return current


# ------------------------------------------------------------------ 跑题检测


def script_coverage(script: Any, plan: dict) -> float:
    """用户内容里的字有多少真的出现在分镜里（按 2 字滑窗算重合率，0~1）。

    免费小模型（glm-4v-flash 这类）有个典型毛病：prompt 说「忠实改编」，它照样
    自己编一个完整故事 —— 用户说「起床伸懒腰、闺蜜做饭、搂腰脸红」，
    它给你《迷雾中的舞者》：都市夜景、湖边、两个陌生人对视。
    光看 JSON 格式完全合法，只有比内容才知道跑题了。这个分数就是那道闸门。
    """
    clean = _punct_strip("" if script is None else str(script))
    grams = {clean[i : i + 2] for i in range(len(clean) - 1)}
    # 太短的内容（「一只猫钓鱼」）没有足够的字可比，别误伤
    if len(grams) < 6:
        return 1.0

    hay = "".join([
        str(plan.get("title") or ""),
        *[f"{c.get('name')}{c.get('desc')}" for c in plan.get("characters") or []],
        *[
            f"{s.get('scene')}{s.get('motion')}{s.get('dialogue')}{s.get('endState') or ''}"
            for s in plan.get("shots") or []
        ],
    ])
    hit = sum(1 for gram in grams if gram in hay)
    return hit / len(grams)


# ------------------------------------------------------------------ 主链路


async def direct_storyboard(
    *,
    script: Any = None,
    shot_count: Any = None,
    style: Any = None,
    want_video: bool = True,
    photo_ids: list | None = None,
    budget_ms: Any = None,
    on_progress: Callable[[dict], None] | None = None,
) -> dict:
    """剧本 → 分镜结构。

    返回 `{title, style, characters[], shots[], loyalty, script, photos}`
      loyalty = `{score, retried, weak}` —— 和原文的重合度，weak 表示「可能跑题了」
    只出文字，不创建任何任务 —— 铺节点、点生成都是前端的事。
    """
    content = str(script or "").strip()
    if len(content) < 6:
        raise ValueError("先贴一段剧本，或者写一句题材（至少 6 个字）")

    def report(stage: str, text_value: str) -> None:
        if on_progress:
            try:
                on_progress({"stage": stage, "text": text_value})
            except Exception:  # noqa: BLE001 - 回调出错不影响主流程
                pass

    # 整条链路的**总**时限，不是单次调用时限。
    # 以前每次 ask 都拿 SCRIPT_TIMEOUT_MS（240 秒），而这条链路最坏要 4 次调用
    # （主调用 + 补镜 2 轮 + 跑题重拆，重拆里还可能再补 2 轮），串起来能到十分钟，
    # 前端 300 秒早就断了 —— 用户看到的就是「signal timed out」。
    # 现在所有调用共享这一个预算，快到点了就不再补镜、不再重拆，先把手上这版交出去。
    try:
        budget = max(60000, round(float(budget_ms)))
    except (TypeError, ValueError):
        budget = DIRECTOR_BUDGET_MS
    deadline = now_ms() + budget

    def left() -> int:
        return deadline - now_ms()

    want = max(MIN_SHOTS, min(MAX_SHOTS, round(float(shot_count or DEFAULT_SHOTS)) if shot_count else DEFAULT_SHOTS))
    use_style = str(style or "").strip()[:300] or DEFAULT_DIRECTOR_STYLE

    # ── 第一层：要不要先让本地 GPT 写一版剧本 ───────────────────────────────
    # 分工的理由（实测）：豆包拆 JSON 分镜很稳，但**写故事很平** —— 给它一句题材，
    # 它拆出来的就是平铺直叙的几个动作；GPT 反过来，故事有层次，可它守 JSON 格式
    # 容易跑偏。所以让 GPT 只写「人话剧本」，豆包只做「剧本 → JSON」。
    #
    # 只在用户给的**不是**剧本（一句题材 / 几句话）时才写：用户已经贴了整份剧本，
    # 再让 GPT 改写一遍就是画蛇添足，还会把他写的东西改掉。
    script_source = "user"  # user = 用户自己给的就是剧本；gpt = GPT 现写的
    script_note = ""        # 写给用户看的一句说明（失败也如实说）
    want_gpt_script = script_engine() == "codex" and not is_script_like(content)
    if want_gpt_script:
        # 写剧本要 15~40 秒，预算里必须先扣掉；剩下的才是拆分的
        write_budget = min(120000, max(30000, left() - 60000))
        try:
            report("script", "正在用本地 GPT 写剧本…")
            out = await write_script_with_gpt(
                idea=content, shot_count=want, style=use_style, timeout_ms=write_budget,
            )
            content = out["script"]
            script_source = "gpt"
            script_note = "题材是本地 GPT 写成剧本的"
        except Exception as err:  # noqa: BLE001 - 写剧本失败不能把整条链路拖死
            # 退回「拿题材直接拆」，并把原因说清楚
            script_note = f"本地 GPT 没能写剧本（{err}），这次直接按你给的题材拆"
            report("script", script_note)

    # 跟 build_prompt 里同一套口径：镜头清单（几件事）也算剧本，不能只看字数，
    # 否则「起床→拉窗帘→下楼做饭→搂腰」这种 70 字的清单会被当成一句话题材，
    # 温度给到 0.85，模型就自由发挥编个不相干的故事
    is_full_script = is_script_like(content)
    photos = collect_photo_refs(photo_ids, MAX_CHARACTERS)
    photo_error = ""
    # 图还发不发给模型。纯文本模型接不了图（直接 400），那次退回来之后就不再发了 ——
    # 但 photos 本身要留着：它们仍然会被挂成角色的定妆照（长相以用户的图为准），
    # 只是角色 desc 只能按用户给的文字写，不再「照着图描述」。
    photos_attached = len(photos) > 0

    def build() -> str:
        return build_prompt(
            script=content[:8000],
            shot_count=want,
            style=use_style,
            want_video=bool(want_video),
            photo_count=len(photos),
            photos_attached=photos_attached,
        )

    # 编导引擎：默认走配置好的 OpenAI 兼容模型；也可以切到本地豆包客户端。
    # 豆包这条路的代价是「必须开着豆包窗口」，好处是完全白嫖，而且实测它写分镜
    # 比免费对话模型听话得多（不会自己编故事、不会漏 place）。
    engine = director_engine()
    use_doubao = engine == "doubao"
    # 有豆包视频任务在跑时不能抢窗口：编导一点「新对话」，那个视频任务就再也等不到自己的片子
    if use_doubao and doubao_video_busy():
        raise ValueError(
            "豆包正在生成视频，这会儿不能拿它拆剧本（会把视频任务那个对话切走）。"
            "等视频出完再拆，或者在「剧本模型」里临时切回云端模型。"
        )

    async def ask(extra: str | None = None, temperature: float | None = None) -> str:
        prompt = f"{build()}\n\n{extra}" if extra else build()
        if use_doubao:
            # 带 extra 的是「补镜 / 重拆」这种追问：不能点「新对话」，
            # 否则豆包忘了上一版分镜，「上面那版只有 2 镜」这句它就听不懂了。
            # 豆包一个来回实测 6~20 秒，给足 90 秒；补镜那几轮也共用这个上限
            return await ask_doubao(
                prompt=prompt,
                new_chat=not extra,
                timeout_ms=max(45000, min(120000, left() - 8000)),
                on_progress=(lambda msg: report("director", msg)) if on_progress else None,
            )
        # 长剧本或有人物图时是要「照着写」，温度压低一点，少点自由发挥
        return await chat_script(
            prompt=prompt,
            temperature=temperature if temperature is not None else (
                0.6 if (is_full_script or photos) else 0.85
            ),
            images=[p["dataUri"] for p in photos] if photos_attached else [],
            # 每次调用只能用「剩下多少预算」，不能每次重新拿满 240 秒
            timeout=max(20000, left() - 8000),
        )

    # 纯文本模型接不了图片（直接 400）。图丢了也要把分镜拆出来 ——
    # 别让「想贴张图」变成整条链路挂掉，所以这次不带图再试一遍，并在回复里如实说。
    # 豆包那条路不收图，所以从一开始就不发。
    if use_doubao:
        photos_attached = False

    try:
        parsed = extract_json(await ask())
    except Exception as err:  # noqa: BLE001
        if not photos or use_doubao:
            raise
        photo_error = str(err) or "模型不接受图片"
        # 注意：这里只停止「发图给模型」，不清空 photos ——
        # 清空了角色就挂不上你的图，画布上会去 AI 生一张编出来的定妆照，
        # 而用户要的恰恰是「模型读不了图，那就直接把我的图放上画布」。
        photos_attached = False
        parsed = extract_json(await ask())
    if not parsed:
        raise ValueError("模型没有按要求给出 JSON 分镜，换个模型再试（免费小模型经常不守格式）")

    # 镜头数不够（小模型把角色数当镜头数是常事）→ 接着补，别让它就这么少着交差
    parsed = await top_up_shots(ask=ask, parsed=parsed, want=want, left=left)

    plan = sanitize_plan(parsed, shot_count=want, style=use_style, photos=photos)
    if not plan["shots"]:
        raise ValueError("模型没有给出可用的镜头画面描述，换个模型再试")

    score = script_coverage(content, plan)
    retried = False

    # 跑题了就再来一次：这次把「必须照用户的内容」单独顶到 prompt 末尾，
    # 温度压到 0.3。还不行就认了 —— 但会把 weak 标出来，让上层如实提示用户。
    # 只在剩余预算够跑「重拆 + 再补镜」时才做：不然重拆到一半被前端掐断，
    # 用户拿到的是一条超时报错，比「这版有点跑题」更糟。
    if score < LOYALTY_MIN and left() >= RETRY_MIN_MS:
        try:
            retry_answer = await ask(
                "**上面这一版跑题了。重新拆一次：只写用户内容里真的有人、有事、有场景，不要另编故事。**"
                if is_full_script
                else "**上面这一版跑题了。重新拆一次：只围绕用户写的那件事展开，不要扯到无关的人物和场景。**",
                0.3,
            )
            retry_parsed = extract_json(retry_answer)
            if retry_parsed:
                # 重拆的这一版也可能少给，一样补齐再比
                retry_parsed = await top_up_shots(ask=ask, parsed=retry_parsed, want=want, left=left)
                retry_plan = sanitize_plan(retry_parsed, shot_count=want, style=use_style, photos=photos)
                retry_score = script_coverage(content, retry_plan) if retry_plan["shots"] else 0.0
                if retry_score > score:
                    plan = retry_plan
                    score = retry_score
            retried = True
        except Exception:  # noqa: BLE001 - 重试用尽就用手上这版，别让用户白等
            pass

    from_photo = len([c for c in plan["characters"] if c.get("fromPhoto")])
    return {
        **plan,
        # 这一版剧本是谁写的：用户自己贴的，还是 GPT 现写的（前端要如实标出来，
        # 否则用户会以为「我明明只写了一句题材，它怎么编了这么多」）
        "script": {
            "source": script_source,
            "note": script_note,
            "text": content if script_source == "gpt" else "",
        },
        "loyalty": {"score": score, "retried": retried, "weak": score < LOYALTY_MIN},
        # 参考图这件事要说清楚：
        #   used     —— 模型真的「看着图」写了吗（0 表示模型不读图，desc 是按文字写的）
        #   attached —— 有几张图直接挂成了定妆照（这些是用户给的图，不需要再 AI 生成）
        #   error    —— 模型读不了图时的原因，只用来提示，不代表图没挂上
        "photos": {
            "used": len(photos) if photos_attached else 0,
            "attached": from_photo,
            "error": photo_error,
        },
    }


__all__ = [
    "DIRECTOR_STYLES",
    "DEFAULT_DIRECTOR_STYLE",
    "MAX_CHARACTERS",
    "MIN_SHOTS",
    "MAX_SHOTS",
    "DEFAULT_SHOTS",
    "build_prompt",
    "detect_place",
    "direct_storyboard",
    "director_engine",
    "director_info",
    "doubao_video_busy",
    "is_script_like",
    "parse_shots_answer",
    "sanitize_plan",
    "script_coverage",
    "top_up_shots",
]
