"""视频抽帧与去水印 —— 复刻 `server/video-frames.js`（6080 字节）。

## 为什么需要「末帧接力」

本地豆包 / 智谱这些不要钱（或免费额度）的链路都**没有尾帧参数**，
一段视频的结尾画面是模型自己发挥的，于是「上一段的最后一帧」和「下一段的第一帧」
对不上 —— 成片在每个接缝处都会跳一下。

免费的解法不是去猜结尾，而是**让下一段从上一帧真的接着走**：
出片后把最后一帧抽出来存成输入图，下一段视频拿它当首帧。
接缝处是同一个画面，像素级连续，看起来就是一镜到底。

## ffmpeg 从哪来

用随包内置的那一份（`build/enhancer/tvai/ffmpeg.exe`，与 Topaz 模块共用）。
不要求用户另外装。抽帧失败时抛错，**调用方自己决定要不要降级** ——
抽不出末帧只是没有接力帧，已经生成好的视频不能跟着废掉。

## 豆包水印为什么只能本地擦

图片链路里响应会另给一条 `image_ori_raw`（干净原版），视频**没有对应物** ——
实测 `download_url` / `main_url` / `backup_url_1` 全都带 `lr=video_gen_watermark_dyn`，
把这个参数改掉或删掉，下载回来的字节**完全相同**，服务端根本不认它。所以只能本地擦。

用 `removelogo` 而不是 `delogo`：随包 ffmpeg 是精简版，没有 delogo，但有 removelogo。
removelogo 要一张遮罩图，遮罩画对了效果比 delogo 还干净（delogo 在纹理上会留横向条纹）。
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import shutil
import time
from pathlib import Path

from .config import ROOT
from .store import store

log = logging.getLogger(__name__)

#: 从结尾往回退多少秒取帧：太靠后可能取到黑场，0.08 秒足够避开
TAIL_OFFSET = "0.08"

_probe_cache: dict | None = None


# ============================================================ 找 ffmpeg


def _enhancer_dir() -> Path:
    """随包内置的引擎目录。

    打包后 —— `resources/enhancer`；开发时 —— 项目内的 `build/enhancer`。
    里面是 `tvai/`（ffmpeg.exe + 依赖 DLL）与 `models/`（超分模型）。
    软件自带，不再依赖外部装 Topaz。

    这里按「文件在不在」判断而不是只看环境变量 —— 打包与否路径不同，
    但两个候选目录都可以直接试。
    """
    for candidate in (Path(ROOT) / "build" / "enhancer", Path(ROOT) / "resources" / "enhancer"):
        if (candidate / "tvai" / "ffmpeg.exe").exists():
            return candidate
    return Path(ROOT) / "build" / "enhancer"


def _topaz_dirs_under(root: Path) -> list[Path]:
    """列出某个根目录下像「Topaz Video AI」的文件夹（新版本排前面）"""
    try:
        if not root.exists():
            return []
        found = [d for d in root.iterdir() if d.is_dir() and re.search(r"topaz\s*video\s*ai", d.name, re.I)]
        return sorted(found, reverse=True)  # v6.0.2 排在 v6.0.0 前面
    except OSError:
        return []


def _find_ffmpeg(dirs: list[Path]) -> str:
    for directory in dirs:
        for name in ("ffmpeg.exe", "ffmpeg"):
            exe = directory / name
            if exe.exists():
                return str(exe)
    return ""


def _find_models_dir() -> Path:
    """模型目录（含 tvai.tz / *.json），跑的时候要用环境变量指给它。随包内置的排最前"""
    candidates = [_enhancer_dir() / "models"]
    program_data = os.environ.get("ProgramData") or os.environ.get("PROGRAMDATA") or r"C:\ProgramData"
    candidates.append(Path(program_data) / "Topaz Labs LLC" / "Topaz Video AI" / "models")
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        candidates.append(Path(local_app_data) / "Topaz Labs LLC" / "Topaz Video AI" / "models")
    for directory in candidates:
        if (directory / "tvai.tz").exists() or (directory / "ahq-12.json").exists():
            return directory
    return candidates[0]


def resolve_topaz(*, force: bool = False) -> dict:
    """找要用的 ffmpeg.exe。顺序：**随包内置 → 配置/环境变量填的 → 常见盘根目录扫一遍**。

    找不到就返回空串，界面上会提示「引擎缺失」并让用户填路径。
    """
    global _probe_cache
    if _probe_cache is not None and not force:
        return _probe_cache

    topaz_cfg = store.topaz_local or {}
    custom = str(topaz_cfg.get("dir") or os.environ.get("TOPAZ_DIR") or "").strip()

    # ① 随包内置的引擎优先 —— 软件自带一份，不依赖用户另外装 Topaz
    bundled_root = _enhancer_dir()
    ffmpeg = _find_ffmpeg([bundled_root / "tvai", bundled_root])
    directory = str(Path(ffmpeg).parent) if ffmpeg else ""
    bundled = bool(ffmpeg)

    # ② 内置缺失时再回头找本机装的 Topaz（内置文件被安全软件清掉 / 手动指定目录的情况）
    if not ffmpeg:
        roots = [custom, "D:\\", "C:\\", "E:\\", r"C:\Program Files", r"C:\Program Files (x86)"]
        for root in [r for r in roots if r]:
            ffmpeg = _find_ffmpeg([Path(root), *_topaz_dirs_under(Path(root))])
            if ffmpeg:
                directory = str(Path(ffmpeg).parent)
                break

    models_dir = _find_models_dir()
    _probe_cache = {
        "found": bool(ffmpeg),
        "ffmpeg": ffmpeg,
        "dir": directory,
        "bundled": bundled,
        "models": str(models_dir),
        "modelsReady": (models_dir / "tvai.tz").exists(),
    }
    return _probe_cache


def set_topaz_dir(directory: str) -> dict:
    """手动指定 Topaz 安装目录（探测不到时用）；传空则恢复自动探测"""
    store.topaz_local["dir"] = str(directory or "").strip()
    store.mark_dirty("topaz_local")
    store.save()
    store.flush()
    return resolve_topaz(force=True)


# ============================================================ 调 ffmpeg


async def _run_ffmpeg(args: list[str], *, timeout_s: float = 60, cwd: str | None = None) -> tuple[int, str]:
    """跑一次 ffmpeg，返回 (返回码, stderr)。

    ffmpeg 把「视频信息」打在 stderr 上，而且 `ffmpeg -i` 在没有输出文件时
    **固定返回非 0** —— 这是正常现象，不能当失败。所以返回码交给调用方判断。
    """
    exe = resolve_topaz()["ffmpeg"]
    if not exe:
        raise RuntimeError("没找到 ffmpeg（增强引擎目录里的 ffmpeg.exe 不见了）")

    process = await asyncio.create_subprocess_exec(
        exe,
        *args,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    try:
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise RuntimeError(f"ffmpeg 执行超时（{timeout_s} 秒）")
    return process.returncode, (stderr or b"").decode("utf-8", "replace")


async def probe_duration(video_path: str) -> float:
    """读视频时长（秒）。ffmpeg 把信息打在 stderr 上，只能从里面抠。
    拿不到就返回 0，调用方按 0 处理（等于从头取一帧，至少不会崩）。
    """
    try:
        _, stderr = await _run_ffmpeg(["-i", video_path], timeout_s=30)
    except Exception:  # noqa: BLE001 - 读不到时长不是致命问题
        return 0.0
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", stderr)
    if not match:
        return 0.0
    return int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3))


async def extract_last_frame(video_path: str, out_path: str) -> str:
    """把视频最后一帧抽成 PNG。

    `video_path` 是已落盘的 mp4，`out_path` 是要写到的 png 路径
    （调用方负责给一个不冲突的名字）。
    """
    if not resolve_topaz()["ffmpeg"]:
        raise RuntimeError("没找到 ffmpeg（增强引擎目录里的 ffmpeg.exe 不见了）")

    try:
        # 首选：-sseof 从末尾定位，不用扫整片，最快。必须放在 -i 之前（输入定位）
        await _run_ffmpeg(
            ["-y", "-sseof", f"-{TAIL_OFFSET}", "-i", video_path, "-frames:v", "1", "-q:v", "2", out_path]
        )
    except Exception:  # noqa: BLE001 - 有些容器不支持从尾部 seek，退一步按秒定位
        duration = await probe_duration(video_path)
        at = f"{max(0.0, duration - float(TAIL_OFFSET)):.3f}"
        await _run_ffmpeg(["-y", "-ss", at, "-i", video_path, "-frames:v", "1", "-q:v", "2", out_path])

    out = Path(out_path)
    if not out.exists() or out.stat().st_size < 1024:
        raise RuntimeError("ffmpeg 没抽出最后一帧（可能视频还没写完或格式不认识）")
    return out_path


async def strip_doubao_watermark(video_path: str) -> dict:
    """豆包视频右下角那块「豆包 AI生成」水印的去除。

    位置是实测的：模板匹配 20 帧里 16 帧命中，横向只漂 22px、纵向 3px、末尾淡出，
    也就是固定在右下角 —— 固定遮罩够用。1280x720 下框是 x1008 y602 w260 h106。

    **失败不抛错**：擦不掉就保留带水印的原片，比把已经出好的片子弄没了好。
    """
    if not resolve_topaz()["ffmpeg"]:
        return {"ok": False, "reason": "没找到 ffmpeg"}
    if not Path(video_path).exists():
        return {"ok": False, "reason": "视频文件不存在"}

    # 1) 问出实际分辨率：竖版 1080x1920 和横版 1280x720 的水印框比例一样，但像素坐标不同
    size = None
    try:
        _, stderr = await _run_ffmpeg(["-i", video_path], timeout_s=30)
        match = re.search(r",\s(\d{2,5})x(\d{2,5})\s", stderr)
        if match:
            size = (int(match.group(1)), int(match.group(2)))
    except Exception as err:  # noqa: BLE001 - 返回非 0 是常态，信息在 stderr 里
        match = re.search(r",\s(\d{2,5})x(\d{2,5})\s", str(err))
        if match:
            size = (int(match.group(1)), int(match.group(2)))
    if not size:
        return {"ok": False, "reason": "读不出视频分辨率"}
    width, height = size

    # 水印框按实测比例换算（1280x720 下是 1008,602,260,106），四周再留一点余量吃住那 22px 漂移
    box_w = round(width * 0.2125)
    box_h = round(height * 0.1583)
    box_x = round(width * 0.7812)
    box_y = round(height * 0.8285)
    # 别越界：遮罩框不能超出画面
    box_w = min(box_w, width - box_x - 2)
    box_h = min(box_h, height - box_y - 2)
    if box_w < 8 or box_h < 8:
        return {"ok": False, "reason": "算出来的遮罩框太小"}

    tmp_dir = Path(os.environ.get("TEMP") or os.environ.get("TMP") or "/tmp") / "lumen-wm"
    try:
        tmp_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return {"ok": False, "reason": "建不了临时目录"}

    stamp = f"{int(time.time() * 1000)}-{random.randrange(36**6):06x}"
    mask_name = f"mask-{stamp}.png"
    out_name = f"clean-{stamp}.mp4"
    mask_path = tmp_dir / mask_name
    out_path = tmp_dir / out_name

    try:
        # 2) 用 lavfi 现场画遮罩：黑底 + 白框。不用带一张 png 进仓库，也不用 python
        await _run_ffmpeg(
            [
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"color=c=black:s={width}x{height}",
                "-vf",
                f"drawbox=x={box_x}:y={box_y}:w={box_w}:h={box_h}:color=white:t=fill",
                "-frames:v",
                "1",
                mask_name,
            ],
            timeout_s=30,
            cwd=str(tmp_dir),
        )

        # 3) 擦。removelogo 只吃一个视频输入 + filename= 参数（双输入写法会报 Invalid argument）。
        #    还有个坑：filename= 里不能出现带冒号的 Windows 绝对路径（D:\... 会被滤镜解析器
        #    当成非法字符），所以把 cwd 切到遮罩所在目录、只传文件名。
        #    编码器优先显卡（快，10 秒片子约 15 秒），没有显卡再退 QSV，都没有就只能放弃
        encoders = [
            ("h264_nvenc", ["-preset", "p5", "-cq", "19"]),
            ("h264_qsv", ["-global_quality", "20"]),
        ]
        encoded = False
        for encoder, encoder_args in encoders:
            try:
                await _run_ffmpeg(
                    [
                        "-y",
                        "-i",
                        str(Path(video_path).resolve()),
                        "-vf",
                        f"removelogo=filename={mask_name}",
                        "-c:v",
                        encoder,
                        *encoder_args,
                        "-c:a",
                        "copy",
                        out_name,
                    ],
                    timeout_s=600,
                    cwd=str(tmp_dir),
                )
                if out_path.exists() and out_path.stat().st_size > 1024:
                    encoded = True
                    break
            except Exception:  # noqa: BLE001 - 换下一个编码器
                continue
        if not encoded:
            return {"ok": False, "reason": "没有可用的硬件编码器"}

        # 4) 换掉原文件。临时目录和项目可能不在同一个盘（C: vs D:），rename 会抛 EXDEV，
        #    所以先复制到同目录再改名 —— 同目录改名是原子的，中途断了也不会留下半个文件
        swap = Path(f"{video_path}.clean")
        shutil.copyfile(out_path, swap)
        os.replace(swap, video_path)
        return {"ok": True, "box": {"x": box_x, "y": box_y, "w": box_w, "h": box_h}, "size": {"w": width, "h": height}}
    except Exception as err:  # noqa: BLE001
        return {"ok": False, "reason": str(err)[:200]}
    finally:
        for leftover in (mask_path, out_path):
            try:
                leftover.unlink()
            except OSError:
                pass
