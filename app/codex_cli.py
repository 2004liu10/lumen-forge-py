"""本地 Codex 桥（本地 GPT）—— 逐函数复刻 `server/providers/codex-cli.js`（6276 字节）。

## 为什么要这一层

用户机器上装着 Codex 桌面版（OpenAI 的本地 CLI，登录态就是他自己的 ChatGPT 账号），
它能 `codex exec` 非交互跑一次，把模型的回复以 JSONL 事件流吐回来。于是「写剧本」
这一步可以完全在本地完成，不用另配 API Key。

定位：**只负责写剧本（文字），不碰分镜结构**。

## 已知取舍（照抄原版注释）

- `codex exec` 一次约 10~20 秒（首轮要加载上下文），比直接调 API 慢，但完全白嫖；
- 输出走 JSONL 事件流，只认 `item.completed` 里 `type == 'agent_message'` 的最后一条 ——
  中间的过程消息（思考、工具调用）不算答案；
- 实测偶发输出尾部会粘一个 `</|fim_suffix|>` 之类的模型残留，统一在这里洗掉；
- 用 `--ephemeral` 不落会话文件，用 `-s read-only` 禁止它动文件系统，`-C` 指到临时目录，
  避免它在项目目录里加载 AGENTS.md 之类的规则。

## Python 侧差异

原版用 `child_process.spawn` + 回调；这里用 `asyncio.create_subprocess_exec`。
`probe_codex` 里的「8 秒拿不到版本号就 kill」用 `asyncio.wait_for` 实现，语义一致。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import tempfile
from pathlib import Path

#: 一次 exec 的默认上限：写一版剧本实测 10~20 秒，给足 3 分钟
DEFAULT_TIMEOUT_MS = 180000
#: 事件流里可能混进非 JSON 的调试行，最多留多少字节用来报错
RAW_KEEP = 1200

#: 尾部残留的特殊 token，只在**末尾**剪，正文里正常出现的尖括号不动
_TAIL_NOISE_A = re.compile(r"(</?\|[a-z_]+\|>)+$", re.I)
_TAIL_NOISE_B = re.compile(r"(<\|[a-z_]+\|>)+$", re.I)


# ============================================================ 找 exe


def codex_exe_candidates() -> list[str]:
    """Codex 可能的安装位置。

    桌面版装在 `LocalAppData\\OpenAI\\Codex\\bin\\<hash>\\codex.exe`，
    npm 装的在 `Roaming\\npm`（那是个 .cmd/.ps1 包装，spawn 起来麻烦，这里优先认 exe）。
    """
    out: list[str] = []
    env_exe = os.environ.get("CODEX_EXE")
    if env_exe:
        out.append(env_exe)

    local = os.environ.get("LOCALAPPDATA")
    if local:
        bin_dir = Path(local) / "OpenAI" / "Codex" / "bin"
        try:
            for name in sorted(os.listdir(bin_dir)):
                out.append(str(bin_dir / name / "codex.exe"))
        except OSError:
            pass  # 没装桌面版就是没有这个目录

    appdata = os.environ.get("APPDATA")
    if appdata:
        out.append(str(Path(appdata) / "npm" / "node_modules" / "@openai" / "codex" / "bin" / "codex.exe"))
    return out


def find_codex_exe() -> str:
    """找出 codex.exe；找不到返回空串"""
    for candidate in codex_exe_candidates():
        if not candidate:
            continue
        try:
            if Path(candidate).exists():
                return candidate
        except OSError:
            continue  # 权限异常，继续找下一个
    return ""


# ============================================================ 事件流解析


def pick_last_message(raw: str | None) -> str:
    """把 stdout 的 JSONL 事件流收成「最后一条助手消息」"""
    last = ""
    for line in re.split(r"\r?\n", str(raw or "")):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") != "item.completed":
            continue
        item = event.get("item") or {}
        if not isinstance(item, dict) or item.get("type") != "agent_message":
            continue
        body = str(item.get("text") or "")
        if body.strip():
            last = body
    return last


def strip_tail_noise(value: str | None) -> str:
    """模型偶尔会在结尾粘一段自己的特殊 token（实测出现过 `</|fim_suffix|>`）"""
    text = str(value or "")
    text = _TAIL_NOISE_A.sub("", text)
    text = _TAIL_NOISE_B.sub("", text)
    return text.strip()


# ============================================================ 探测


async def probe_codex() -> dict:
    """这台机器上 Codex 能不能用：给界面「检测」按钮和报错文案用。

    版本能打出来只说明 exe 是好的；能不能真的跑一轮要看登录态，那要花十几秒，
    所以这里只报「装了」，真跑失败会在 `ask_codex` 的报错里说清楚。
    """
    exe = find_codex_exe()
    if not exe:
        return {"ok": False, "reason": "no-exe", "exe": "", "version": ""}

    version = ""
    try:
        proc = await asyncio.create_subprocess_exec(
            exe,
            "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # Windows 上不弹黑窗
            **_hide_window_kwargs(),
        )
    except (OSError, ValueError):
        return {"ok": False, "reason": "no-run", "exe": exe, "version": ""}

    try:
        out, _err = await asyncio.wait_for(proc.communicate(), timeout=8.0)
        version = (out or b"").decode("utf-8", "replace").strip()
    except (asyncio.TimeoutError, OSError):
        try:
            proc.kill()
        except (ProcessLookupError, OSError):
            pass  # 已退出
        try:
            await proc.wait()
        except OSError:
            pass
    return {"ok": bool(version), "reason": "" if version else "no-run", "exe": exe, "version": version}


def _hide_window_kwargs() -> dict:
    """Windows 上给子进程加 `CREATE_NO_WINDOW`，避免弹控制台窗口。

    `creationflags` 只在 Windows 的 asyncio ProactorEventLoop 上支持；
    其它平台传了会 TypeError，所以按平台给。
    """
    if sys.platform != "win32":
        return {}
    flags = 0x08000000  # CREATE_NO_WINDOW
    return {"creationflags": flags}


# ============================================================ 跑一轮


async def ask_codex(
    *,
    prompt: str,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    cwd: str | None = None,
) -> str:
    """让本地 GPT 写一段东西，返回它的文字回复。

    `prompt` 走 stdin（参数写 `-`）：剧本动辄几千字，走命令行参数在 Windows 上
    容易被长度限制和引号转义搞坏。
    """
    text = str(prompt or "").strip()
    if not text:
        raise ValueError("提示词不能为空")

    exe = find_codex_exe()
    if not exe:
        raise RuntimeError(
            "没找到本地 Codex（codex.exe）。装一个 Codex 桌面版并登录，"
            "或者用环境变量 CODEX_EXE 指定 codex.exe 的完整路径；"
            "不想用 GPT 写剧本就把「剧本模型」的引擎切回云端模型。"
        )

    # 单独给个空目录当工作根：不让它在项目里读到 AGENTS.md / 代码，
    # 只当一台「会写字的机器」用。
    work_dir = cwd or str(Path(tempfile.gettempdir()) / "lumen-forge-codex")
    try:
        Path(work_dir).mkdir(parents=True, exist_ok=True)
    except OSError:
        pass  # 已存在 / 无权限都无所谓

    args = [
        "exec",
        "--json",  # 事件流，好稳定地抠最后一条回复
        "--skip-git-repo-check",
        "--ephemeral",  # 不落会话文件，别在用户目录里堆垃圾
        "-s",
        "read-only",  # 只许读，不许它动任何文件
        "--color",
        "never",
        "-C",
        work_dir,
        "-",  # 提示词从 stdin 读
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            exe,
            *args,
            cwd=work_dir,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **_hide_window_kwargs(),
        )
    except (OSError, ValueError) as err:
        raise RuntimeError(f"本地 Codex 启动失败：{err}") from err

    timeout_s = max(15.0, timeout_ms / 1000)

    async def _communicate() -> tuple[bytes, bytes]:
        try:
            out, err = await proc.communicate(input=text.encode("utf-8"))
            return out, err
        except (BrokenPipeError, ConnectionResetError):
            # 进程秒退（未登录 / 参数不认），stdin 写不进去；错误会从 returncode 报出来
            try:
                out, err = await proc.communicate()
            except OSError:
                return b"", b""
            return out, err

    try:
        stdout_b, stderr_b = await asyncio.wait_for(_communicate(), timeout=timeout_s)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except (ProcessLookupError, OSError):
            pass
        try:
            await proc.wait()
        except OSError:
            pass
        raise RuntimeError(
            f"本地 GPT 一直没有回复（超过 {round(timeout_ms / 1000)} 秒），换个引擎或稍后再试"
        ) from None

    stdout = (stdout_b or b"").decode("utf-8", "replace")
    stderr = (stderr_b or b"").decode("utf-8", "replace")
    code = proc.returncode

    answer = strip_tail_noise(pick_last_message(stdout))
    if answer:
        return answer

    detail = re.sub(r"\s+", " ", stderr or stdout).strip()[:RAW_KEEP]
    # 未登录 / 额度用完 / 网络不通都会走到这里，把原文带出去让用户能自己判断
    raise RuntimeError(
        f"本地 GPT 没有返回内容{f'（codex 退出码 {code}）' if code else ''}"
        f"{f'：{detail}' if detail else '。检查 Codex 是不是已登录'}"
    )
