"""视频生成任务队列 —— 逐函数复刻 `server/video-jobs.js`（528 行）。

## 它和另外两套调度都独立

- **不复用 `scheduler.py`**：它的账号池、RH 币额度、`quotaUsed` 全是 RunningHub 专用。
- **不复用 `image_jobs.py`**：图片是「一次请求拿到结果」，视频是
  「提交 → 长时间轮询 → 下载」两阶段。

## 轮询用「状态 + 时间戳」驱动

定时器每 3 秒跳一次，任务自己带 `nextPollAt`，到点才动它。
好处是**重启接管是白送的** —— 状态存在 SQLite 里，新进程起来后
`status='polling'` 且 `nextPollAt` 已过期的任务会被自然捡起来继续轮询
（0.6.3 那次「平台上成功了、本地永远卡 running」的坑，从设计上避免）。

## 几个非直觉的约束（都是被真实故障逼出来的）

1. **本地豆包同一时间只能跑一个任务**：它是个「一个客户端窗口」，后提交的任务会先点
   「新对话」，前一个任务正等着的那个对话就被切走了 —— 它再也等不到自己的视频，只能超时失败。
   见 `doubao_busy_except`。
2. **只在「明确是额度问题」时才换豆包账号**：内容违规 / 不支持生成 换号也没用，
   切来切去只是把两个号都搞脏。见 `switch_account_for_quota`。
3. **`submitting` 状态重启后的处理要分两种**：没拿到 `remoteTaskId` 说明请求发了但结果未知，
   重提可能重复扣费 → 直接判失败；已有 `remoteTaskId` 说明其实提交成功了 → 转成轮询。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from .ids import next_id, now_ms
from .models import new_video_input, new_video_task
from .providers import videogen
from .providers.doubao_bridge import switch_doubao_account
from .scheduler import get_paths, mask_key
from .shared_files import unlink_if_unused
from .store import store
from .video_frames import extract_last_frame, strip_doubao_watermark

log = logging.getLogger(__name__)

TICK_MS = 3000
#: 三家都建议 15~30 秒查一次，取 15 秒
POLL_INTERVAL_MS = 15000
#: 连续查询失败这么多次才判失败，避免网络抖动直接杀掉任务
MAX_POLL_ERRORS = 10

#: 会被 worker 捡起来继续推进的状态
ACTIVE = {"queued", "polling", "downloading"}
#: 正在占着厂商那边资源的状态（不含 queued：还没提交，不算占位）
IN_FLIGHT = {"submitting", "polling", "downloading"}


# ============================================================ 查询 / 快照


def find_video_model(model_id: str | None) -> dict | None:
    if not model_id:
        return None
    return store.find("video_models", id=model_id)


def find_video_task(task_id: str | None) -> dict | None:
    if not task_id:
        return None
    return store.find("video_tasks", id=task_id)


def find_video_input(input_id: str | None) -> dict | None:
    if not input_id:
        return None
    return store.find("video_inputs", id=input_id)


def video_model_snapshot(model: dict) -> dict:
    """对外快照：API Key 只回显掩码，避免整串密钥反复在网络上传输"""
    return {
        **model,
        "apiKey": mask_key(model.get("apiKey")),
        "hasKey": bool(model.get("apiKey")),
        # 前端靠它决定「尾帧那张图要不要上传」：不支持的模型传上去也是白传
        "supportsLastFrame": videogen.last_frame_supported(model),
        # 豆包这类没有尾帧参数的，第二张图也能当「这段要走到哪」的目标画面一起塞
        "takesSecondImage": videogen.takes_second_image(model),
    }


def video_input_snapshot(input_: dict) -> dict:
    return {
        "id": input_.get("id"),
        "fileName": input_.get("fileName"),
        "originalName": input_.get("originalName") or input_.get("fileName"),
        "localUrl": input_.get("localUrl"),
        "mimeType": input_.get("mimeType"),
        "size": input_.get("size"),
        "source": input_.get("source") or "",
        "shared": bool(input_.get("shared")),
        "createdAt": input_.get("createdAt"),
    }


# ============================================================ 输入图库


def _register_video_input(
    *,
    buffer: bytes,
    original_name: str,
    mime_type: str,
    ext: str,
    source: str | None,
    source_task_id: str | None = None,
    source_image_id: str | None = None,
) -> dict:
    stored_name = f"{next_id('vi')}{ext}"
    local_path = get_paths().video_inputs / stored_name
    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.write_bytes(buffer)
    input_ = new_video_input(
        file_name=stored_name,
        original_name=original_name or stored_name,
        local_path=str(local_path),
        local_url=f"/files/video-inputs/{stored_name}",
        mime_type=mime_type,
        size=len(buffer),
        source=source,
        source_task_id=source_task_id,
        source_image_id=source_image_id,
    )
    store.video_inputs.append(input_)
    store.mark_dirty("video_inputs")
    return input_


def create_video_input_from_upload(*, buffer: bytes, original_name: str) -> dict:
    """本地上传的输入图：按扩展名判定格式，非图片直接拒绝"""
    ext = Path(original_name or "").suffix.lower()
    mime_type = videogen.INPUT_MIME_TYPES.get(ext)
    if not mime_type:
        raise ValueError(f"输入图只支持 {' / '.join(videogen.INPUT_MIME_TYPES)} 格式")
    return _register_video_input(
        buffer=buffer, original_name=original_name, mime_type=mime_type, ext=ext, source="upload"
    )


def create_video_input_from_image(task: dict | None, image: dict) -> dict:
    """自用模块的生成结果一键送过来。

    **不再复制文件**：直接引用生成结果那一份（同机同盘）。
    删掉生成记录时由 `unlink_if_unused` 数引用决定要不要真删文件，不会把视频输入图搞丢。
    """
    local_path_raw = str(image.get("localPath") or "")
    if not local_path_raw or not Path(local_path_raw).exists():
        raise ValueError("这张图片的本地文件已被删除，无法送去视频模块")
    local_path = Path(local_path_raw).resolve()
    ext = (Path(str(image.get("fileName") or "")).suffix or ".png").lower()
    input_ = new_video_input(
        file_name=local_path.name,
        original_name=str(image.get("fileName") or ""),
        local_path=str(local_path),
        local_url=image.get("localUrl") or f"/files/generated/{local_path.name}",
        mime_type=videogen.INPUT_MIME_TYPES.get(ext, "image/png"),
        size=int(image.get("size") or 0),
        source="fromImage",
        source_task_id=(task or {}).get("id") or "",
        source_image_id=str(image.get("id") or ""),
    )
    input_["shared"] = True  # 标记：是共享引用，不是本目录的独立副本
    store.video_inputs.append(input_)
    store.mark_dirty("video_inputs")
    store.save()
    return input_


def remove_video_input(input_: dict) -> None:
    """删除输入图：先从列表摘掉，再交给 `unlink_if_unused` 数一遍引用决定要不要真删文件"""
    inputs = store.video_inputs
    for index, item in enumerate(inputs):
        if item.get("id") == input_.get("id"):
            del inputs[index]
            break
    # 已经被别人共享的文件不能直接删：统一走 unlink_if_unused
    unlink_if_unused(input_.get("localPath"))
    store.mark_dirty("video_inputs")
    store.save()
    store.flush()


# ============================================================ 任务管理


def _input_snapshot(input_: dict | None) -> dict | None:
    """输入图快照：路径给执行用（读成 data URI 发给厂商），URL 给历史记录展示用。
    首帧和尾帧共用这一套字段。
    """
    if not input_:
        return None
    return {
        "id": input_.get("id"),
        "fileName": input_.get("fileName"),
        "originalName": input_.get("originalName") or input_.get("fileName"),
        "localPath": input_.get("localPath"),
        "localUrl": input_.get("localUrl"),
        "mimeType": input_.get("mimeType"),
        "size": input_.get("size"),
    }


def create_video_task(
    *,
    model_id: str | None,
    prompt: str,
    duration=None,
    ratio=None,
    resolution=None,
    seed=None,
    tier=None,
    input_id: str | None = None,
    end_input_id: str | None = None,
) -> dict:
    model = find_video_model(model_id)
    if not model:
        raise ValueError("请选择视频模型")
    if not model.get("enabled"):
        raise ValueError(f"视频模型「{model.get('name')}」已停用")
    text = str(prompt or "").strip()
    if not text:
        raise ValueError("请填写提示词")
    input_ = find_video_input(input_id)
    if not input_:
        raise ValueError("请先添加输入图片（也可以用自用模块结果图上的「送去视频」）")
    # 尾帧（这一段视频最后一帧要落到的画面）：给了就用，没给就是普通的单图图生视频。
    # 模型不认尾帧时不报错 —— 画布上照样连着两张图，只是画面终点改由提示词描述。
    end_input = find_video_input(end_input_id) if end_input_id else None

    params = {
        "duration": str(duration if duration is not None else (model.get("defaultDuration") or "")),
        "ratio": str(ratio if ratio is not None else (model.get("defaultRatio") or "")),
        "resolution": str(resolution if resolution is not None else (model.get("defaultResolution") or "")),
        "seed": "" if seed is None else str(seed),
    }
    # 本地豆包的内部模型档（2.5 / 2.0 / fast / mini）：别的厂商没有这一层，留空即可
    if tier:
        params["tier"] = str(tier)

    task = new_video_task(
        model=model,
        prompt=text,
        params=params,
        # 快照只留生成时用得到的字段：路径给执行用，URL 给历史记录展示用
        input=_input_snapshot(input_),
        end_input=_input_snapshot(end_input),
    )
    store.video_tasks.append(task)
    store.mark_dirty("video_tasks")
    store.save()
    store.flush()
    return task


def retry_video_task(task: dict) -> dict:
    """重新排队：清掉上次的产物与错误，避免和新的结果混在一起"""
    if task.get("status") in ACTIVE:
        raise ValueError("任务正在进行中，无法重试")
    task.update(
        {
            "status": "queued",
            "error": "",
            "video": None,
            "remoteTaskId": "",
            "remoteStatus": "",
            "remoteVideoUrl": "",
            "nextPollAt": 0,
            "pollErrors": 0,
            "cancelRequested": False,
            # 手动重试是用户新的一轮：把「已试过没额度」的账号清掉，
            # 否则上次额度用完时记下的黑名单会让这次直接放弃换号
            "triedAccounts": [],
            "startedAt": None,
            "finishedAt": None,
        }
    )
    store.mark_dirty("video_tasks")
    store.save()
    store.flush()
    return task


def cancel_video_task(task: dict) -> dict:
    """只支持取消还没提交出去的任务：已提交的请求费用已经产生，让它跑完更有价值"""
    if task.get("status") == "queued":
        task["status"] = "cancelled"
        task["finishedAt"] = now_ms()
        store.mark_dirty("video_tasks")
        store.save()
        store.flush()
        return task
    if task.get("status") in ACTIVE:
        raise ValueError("任务已提交到厂商，无法取消（请求已发出）")
    raise ValueError("任务已结束，无需取消")


# ============================================================ 执行循环


def _fail(task: dict, message: str) -> None:
    task["status"] = "failed"
    task["error"] = message
    task["finishedAt"] = now_ms()
    store.mark_dirty("video_tasks")
    store.save()
    store.flush()


async def switch_account_for_quota(task: dict, model: dict | None) -> bool:
    """豆包免费额度用完时换一个账号再试。

    为什么值得做：豆包桌面版本身就登录着多个账号（左下角账号菜单里能切），
    一个号当天次数用完了，另一个号往往还有 —— 白嫖链路能不能续上，全看这里。

    只在「明确是额度问题」时才换号：内容违规 / 不支持生成 换号也没用，
    切来切去只是把两个号都搞脏。

    已经试过的账号记在 `task.triedAccounts` 里，避免两个号之间来回踢皮球。
    返回 True 表示换到了新号、任务可以重新提交。
    """
    if not model or model.get("provider") != "doubao":
        return False
    tried = list(task.get("triedAccounts") or [])
    task["progress"] = "这个豆包账号今天的免费额度用完了，正在换一个账号重试…"
    store.mark_dirty("video_tasks")
    store.save()
    store.flush()
    try:
        res = await switch_doubao_account(endpoint=model.get("baseUrl") or None, avoid=tried)
    except Exception as err:  # noqa: BLE001 - 换号失败不该让任务崩掉
        res = {"switched": False, "reason": str(err)}

    # 当前这个号也记进 tried：万一换回来还是它，不会再试第二次
    source = str(res.get("from") or "")
    next_tried = list(dict.fromkeys([*tried, *([source] if source else [])]))
    task["triedAccounts"] = next_tried
    if not res.get("switched"):
        store.mark_dirty("video_tasks")
        store.save()
        store.flush()
        return False
    task["accountSwitches"] = int(task.get("accountSwitches") or 0) + 1
    task["switchedTo"] = str(res.get("to") or "")
    store.mark_dirty("video_tasks")
    store.save()
    store.flush()
    return True


def doubao_busy_except(task: dict) -> bool:
    """本地豆包是「一个客户端窗口」：同一时间只允许一个豆包任务在推进，其余排队等着。"""
    model = find_video_model(task.get("modelId"))
    if not model or model.get("provider") != "doubao":
        return False
    for other in store.video_tasks:
        if other.get("id") == task.get("id") or other.get("status") not in ACTIVE:
            continue
        if other.get("status") == "queued":
            continue  # 都还没提交，不算占位
        other_model = find_video_model(other.get("modelId"))
        if other_model and other_model.get("provider") == "doubao":
            return True
    return False


async def submit_task(task: dict) -> None:
    model = find_video_model(task.get("modelId"))
    task["status"] = "submitting"
    task["startedAt"] = task.get("startedAt") or now_ms()
    task["attempts"] = int(task.get("attempts") or 0) + 1
    task["error"] = ""
    task["nextPollAt"] = 0
    store.mark_dirty("video_tasks")
    store.save()
    store.flush()

    if not model:
        _fail(task, "这个任务使用的视频模型已被删除")
        return

    def on_progress(message: str) -> None:
        # 本地豆包桥一步要好几秒（传图、打字、等它接活），把过程写进任务进度
        task["progress"] = str(message or "")[:200]
        store.mark_dirty("video_tasks")

    try:
        res = await videogen.submit_video(
            model=model,
            prompt=str(task.get("prompt") or ""),
            params=task.get("params") or {},
            image=task.get("input"),
            # 尾帧只有支持的模型才会真发出去（见 last_frame_supported），不支持的自动忽略
            end_image=task.get("endInput"),
            on_progress=on_progress,
        )
    except Exception as err:  # noqa: BLE001
        # 额度用完了：换个豆包账号重新提交（只在这一种情况下重试 ——
        # 别的失败重试可能「其实已经提交成功」，重复扣费）
        if getattr(err, "quota", False) and await switch_account_for_quota(task, model):
            task["status"] = "queued"
            task["nextPollAt"] = 0
            task["progress"] = f"已换到账号「{task.get('switchedTo') or '另一个'}」，重新提交…"
            store.mark_dirty("video_tasks")
            store.save()
            store.flush()
            return
        _fail(task, str(err))
        return

    task["remoteTaskId"] = str(res.get("remoteTaskId") or "")
    task["remoteStatus"] = "submitted"
    task["status"] = "polling"
    task["nextPollAt"] = now_ms() + POLL_INTERVAL_MS
    # resolved 里带着「面板上实际生效的参数」（本地豆包才有；别的厂商就是接口参数）
    applied = res.get("applied")
    if applied:
        task["applied"] = applied
    # 参数没能设进去（豆包改版 / 面板没打开）时把它说清楚，别让人以为真按 30 秒在跑
    note = res.get("note")
    if note:
        task["progress"] = str(note)[:200]
    store.mark_dirty("video_tasks")
    store.save()
    store.flush()


async def poll_task(task: dict) -> None:
    model = find_video_model(task.get("modelId"))
    if not model:
        _fail(task, "这个任务使用的视频模型已被删除")
        return
    try:
        result = await videogen.query_video(model=model, remote_task_id=str(task.get("remoteTaskId") or ""))
    except Exception as err:  # noqa: BLE001
        # 查询失败多为网络抖动，保持轮询而不是直接判失败
        count = int(task.get("pollErrors") or 0) + 1
        task["pollErrors"] = count
        task["error"] = f"查询任务状态失败（第 {count} 次）：{err}"
        if count >= MAX_POLL_ERRORS:
            _fail(task, f"连续 {count} 次查询任务状态失败，已放弃：{err}")
            return
        task["nextPollAt"] = now_ms() + POLL_INTERVAL_MS
        store.mark_dirty("video_tasks")
        store.save()
        store.flush()
        return

    task["remoteStatus"] = str(result.get("raw") or result.get("status") or "")
    if result.get("status") == "success":
        task["pollErrors"] = 0
        task["error"] = ""
        video_url = result.get("videoUrl")
        if not video_url:
            _fail(task, "厂商报告生成成功，但没有返回视频地址")
            return
        task["remoteVideoUrl"] = video_url
        task["status"] = "downloading"
        task["nextPollAt"] = 0
    elif result.get("status") == "failed":
        # 轮询中才发现额度用完（豆包会先接活、过一会儿才说没次数）：
        # 同样换号重来。此时旧任务已经废了，重新提交不会重复扣费。
        if result.get("quota") and await switch_account_for_quota(task, model):
            task["status"] = "queued"
            task["remoteTaskId"] = ""
            task["remoteStatus"] = ""
            task["remoteVideoUrl"] = ""
            task["nextPollAt"] = 0
            task["progress"] = f"已换到账号「{task.get('switchedTo') or '另一个'}」，重新提交…"
            store.mark_dirty("video_tasks")
            store.save()
            store.flush()
            return
        _fail(task, result.get("error") or "厂商返回任务失败")
        return
    else:
        task["pollErrors"] = 0
        task["error"] = ""
        # 豆包一条视频要 5 分钟左右（它自己写着「预计等待 5 分钟」）。
        # 只显示「生成中」太像卡死，把已经等了多久写进去，用户才知道是在跑
        if model.get("provider") == "doubao":
            waited = max(0, round((now_ms() - int(task.get("createdAt") or now_ms())) / 1000))
            human = f"{waited // 60} 分 {waited % 60} 秒" if waited >= 60 else f"{waited} 秒"
            task["progress"] = f"{result.get('raw') or '生成中'} · 已等 {human}"
        task["nextPollAt"] = now_ms() + POLL_INTERVAL_MS
    store.mark_dirty("video_tasks")
    store.save()
    store.flush()


async def register_last_frame(task: dict, video_path: str) -> dict:
    """出片后把最后一帧抽出来登记进输入图库 —— 下一段视频拿它当首帧（末帧接力）。

    这是锦上添花，不是必经步骤：抽帧失败只记日志，绝不把已经出好的片子判失败。
    """
    stored_name = f"{next_id('vi')}.png"
    local_path = get_paths().video_inputs / stored_name
    await extract_last_frame(video_path, str(local_path))
    video = task.get("video") or {}
    base_name = str(video.get("fileName") or "video")
    frame = new_video_input(
        file_name=stored_name,
        original_name=f"{Path(base_name).stem}-last.png",
        local_path=str(local_path),
        local_url=f"/files/video-inputs/{stored_name}",
        mime_type="image/png",
        size=local_path.stat().st_size,
        source="lastFrame",
        source_task_id=task.get("id"),
    )
    store.video_inputs.append(frame)
    store.mark_dirty("video_inputs")
    return frame


async def download_task(task: dict) -> None:
    model = find_video_model(task.get("modelId"))
    try:
        # 带上 model：本地豆包的视频要从豆包页面里取（blob: 地址普通 fetch 拿不到）
        res = await videogen.download_video({"model": model, "url": task.get("remoteVideoUrl")})
        stored_name = f"{next_id('video')}{res.get('ext') or '.mp4'}"
        local_path = get_paths().videos / stored_name
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(res["buffer"])
    except Exception as err:  # noqa: BLE001
        _fail(task, str(err))
        return

    # 豆包出片右下角烧着「豆包 AI生成」水印，图片链路能拿 image_ori_raw 干净原版，
    # 视频没有对应物，只能本地擦。擦不掉不影响这条任务：带水印的片子照样能用
    if model and model.get("provider") == "doubao":
        wiped = await strip_doubao_watermark(str(local_path))
        if not wiped.get("ok"):
            log.warning("[video] 去水印没成功（保留带水印的片子）：%s", wiped.get("reason"))
        else:
            task["watermarkStripped"] = True

    # 擦过水印的话文件已经被整个换掉了，字节数要重新读盘，不然界面上显示的还是擦之前的大小
    try:
        final_size = local_path.stat().st_size
    except OSError:
        final_size = len(res["buffer"])

    task["video"] = {
        "fileName": stored_name,
        "localPath": str(local_path),
        "localUrl": f"/files/videos/{stored_name}",
        "remoteUrl": task.get("remoteVideoUrl") or "",  # 厂商链接会过期，本地副本才是长期保存的
        "size": final_size,
    }
    task["status"] = "success"
    task["error"] = ""
    task["finishedAt"] = now_ms()

    try:
        task["lastFrame"] = await register_last_frame(task, str(local_path))
    except Exception as err:  # noqa: BLE001
        # 抽不出末帧只是没有接力帧（下一段会退回用镜头图），已经出好的片子不能跟着废掉
        log.warning("[video] 抽末帧失败：%s", err)
    store.mark_dirty("video_tasks")
    store.save()
    store.flush()


class _State:
    working = False
    task: asyncio.Task | None = None


def _spawn(coro, *, label: str) -> asyncio.Task:
    t = asyncio.get_running_loop().create_task(coro)

    def _done(fut: asyncio.Task) -> None:
        if fut.cancelled():
            return
        err = fut.exception()
        if err is not None:
            log.warning("[video] %s 未捕获异常：%s", label, err)

    t.add_done_callback(_done)
    return t


async def tick() -> None:
    if _State.working:
        return
    now = now_ms()
    task = next(
        (
            t
            for t in store.video_tasks
            if t.get("status") in ACTIVE and int(t.get("nextPollAt") or 0) <= now
        ),
        None,
    )
    if not task:
        return
    _State.working = True
    try:
        if task.get("status") == "queued":
            # 豆包任务一个一个来：前面那个还在跑就把这条往后挪，别去抢同一个窗口
            if doubao_busy_except(task):
                task["nextPollAt"] = now_ms() + POLL_INTERVAL_MS
                store.mark_dirty("video_tasks")
                store.save()
                store.flush()
                return
            await submit_task(task)
        elif task.get("status") == "polling":
            await poll_task(task)
        elif task.get("status") == "downloading":
            await download_task(task)
    finally:
        _State.working = False


async def _tick_loop() -> None:
    while True:
        try:
            await tick()
        except Exception as err:  # noqa: BLE001
            log.warning("[video] tick 异常：%s", err)
        await asyncio.sleep(TICK_MS / 1000)


def adopt_video_tasks() -> bool:
    """启动时接管遗留任务：

    - `submitting` 且没拿到 `remoteTaskId` → 请求发了但结果未知，重提可能重复扣费，直接判失败
    - `submitting` 但已有 `remoteTaskId` → 其实提交成功了，只是没来得及改状态，转成轮询
    - `polling` / `downloading` → 状态本就在库里，到点会被 `tick` 自然捡起来
    """
    changed = False
    for task in store.video_tasks:
        if task.get("status") != "submitting":
            continue
        changed = True
        if task.get("remoteTaskId"):
            task["status"] = "polling"
            task["error"] = ""
            task["nextPollAt"] = 0
        else:
            task["status"] = "failed"
            task["error"] = "程序重启时这次提交的结果未知，请重新生成（避免重复扣费）"
            task["finishedAt"] = now_ms()
    if changed:
        store.mark_dirty("video_tasks")
        store.save()
        store.flush()
    return changed


def start_video_worker() -> None:
    adopt_video_tasks()
    if _State.task is None or _State.task.done():
        _State.task = _spawn(_tick_loop(), label="video-tick-loop")


async def stop_video_worker() -> None:
    if _State.task is not None and not _State.task.done():
        _State.task.cancel()
        try:
            await _State.task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    _State.task = None
