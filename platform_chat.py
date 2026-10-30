"""平台通道：不填 API Key 也能对话。

原理
----
部署机（或本机）上如果存在 `codebuddy` CLI，它自己带着登录凭证 / 平台代理
（CLI 启动日志里 `apiKeySource: auth.proxy` 就是这个意思）。我们不去逆向
任何网页接口，只是把 CLI 当成一个「本地模型网关」用子进程调度：

    codebuddy -p "<prompt>" --model <id> --output-format stream-json \
              --include-partial-messages --tools "Read,Glob,Grep"

好处
----
- 零 Key：用的是设备上已登录的那份额度，不外传、不落库
- 真流式：CLI 输出 stream-json，逐行解析拿 text_delta
- 有上下文：`--session-id` 建立、`--resume` 续接，等同于官方会话
- 只读工具：只放行 Read/Glob/Grep，禁止 Bash/Write，聊天面板不会误改文件

限制
----
- 只有装了 CLI 且已登录的机器能用；纯 Docker 部署默认没有，走 BYO Key
- 每次对话是一次进程冷启动（约 1~3s 首字延迟）
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import time
from pathlib import Path

TOOLS = os.environ.get("DESK_CHAT_TOOLS", "").strip()

# 默认禁用全部工具：聊天面板要的是「快」和「干净」。
# 想让它能读工作区文件，部署时设 DESK_CHAT_TOOLS="Read,Glob,Grep"（只读，安全）。
# 开启后模型偶尔会尝试调用不存在的 Bash，产生 "Tool does not exist" 噪声，
# 这里在输出层做一次过滤，避免脏文本落到聊天记录里。
_NOISE = re.compile(r'Tool "[^"]{1,40}" does not exist in the current tool set\.?')

# 没有工具时，模型会把「想执行」变成伪工具调用的**文本**（形如
# <tool_calls:opensource><tool_call:opensource>Bash command={...}</tool_call...>），
# 直接渲染就是一团乱码。系统提示里先把话说明白，剩下的靠 _Sanitizer 兜住。
_NO_TOOL_HINT = (
    "你现在处于「对话面板」，没有任何工具：不能执行命令、不能读写文件、不能访问网络。"
    "绝对不要输出 <tool_call> / <tool_calls> 之类的伪工具标记，也不要假装已经执行。"
    "如果用户要求拉取仓库、安装依赖、启动服务，请直接给出可以在终端粘贴执行的命令，"
    "并明确提示「到 💻 终端 标签执行」。"
)

_TOOL_OPEN = "<tool_call"

IDLE_TICK = 15          # 每 15 秒一次心跳
IDLE_LIMIT = 120        # 连续静默 120 秒判定卡死


class _Sanitizer:
    """把流式文本里的伪工具调用标记整块吃掉。

    块可能被切成多个 delta（"<tool_ca" + "lls:…"），所以保留跨片状态；
    未闭合的块一直丢到出现 </tool_call…> 为止，结尾残留的半个标签也不输出。
    """

    HOLD = len(_TOOL_OPEN) - 1

    def __init__(self):
        self.pending = ""
        self.in_block = False

    def feed(self, text: str) -> str:
        s = self.pending + (text or "")
        self.pending = ""
        out: list[str] = []
        while s:
            if self.in_block:
                j = s.find("</tool_call")
                if j < 0:
                    return "".join(out)
                k = s.find(">", j)
                if k < 0:
                    return "".join(out)
                s = s[k + 1:]
                self.in_block = False
                continue

            i = s.find(_TOOL_OPEN)
            if i >= 0:
                out.append(s[:i])
                s = s[i:]
                self.in_block = True
                continue

            keep = 0
            for n in range(min(self.HOLD, len(s)), 0, -1):
                if _TOOL_OPEN.startswith(s[-n:]):
                    keep = n
                    break
            if keep:
                self.pending = s[-keep:]
                out.append(s[:-keep])
            else:
                out.append(s)
            break
        res = _NOISE.sub("", "".join(out))
        # 块闭合后可能还留一个 </tool_calls:xxx> 的尾巴，一并清掉
        return re.sub(r"</?tool_calls?[^>]*>", "", res)

_MODELS_CACHE = {"at": 0.0, "list": []}
_HELP_CACHE = {"at": 0.0, "ok": False}


def cli_path() -> str | None:
    for name in ("codebuddy", "cbc"):
        p = shutil.which(name)
        if p:
            return p
    # nvm 全局目录兜底（systemd / 非登录 shell 下 PATH 可能不全）
    for base in ("/root/.nvm/versions/node", os.path.expanduser("~/.nvm/versions/node")):
        b = Path(base)
        if b.is_dir():
            for d in sorted(b.iterdir(), reverse=True):
                for name in ("codebuddy", "cbc"):
                    p = d / "bin" / name
                    if p.is_file():
                        return str(p)
    return None


def available() -> bool:
    return cli_path() is not None


def models(force: bool = False) -> list[str]:
    """从 CLI 的 --help 里解析受支持的模型 ID（不联网、毫秒级）。"""
    now = time.time()
    if not force and _MODELS_CACHE["list"] and now - _MODELS_CACHE["at"] < 600:
        return _MODELS_CACHE["list"]
    exe = cli_path()
    if not exe:
        return []
    try:
        import subprocess
        out = subprocess.run([exe, "--help"], capture_output=True, text=True,
                             timeout=20).stdout
    except Exception:
        return _MODELS_CACHE["list"]
    m = re.search(r"Currently supported:\s*\(([^)]*)\)", out)
    lst = []
    if m:
        lst = [x.strip() for x in m.group(1).split(",") if x.strip()]
    if lst:
        _MODELS_CACHE.update(at=now, list=lst)
    return lst


async def _read_stream(exe: str, args: list[str], cwd: str):
    """拉起 CLI 并逐行产出 JSON 对象。

    CLI 冷启动或长思考时可能几十秒没有任何输出，浏览器那头的 SSE 会像"卡住"；
    这里每 15 秒发一次 ping，让连接保持活跃，连续 120 秒真没动静才判定失败。
    """
    proc = await asyncio.create_subprocess_exec(
        exe, *args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "NO_COLOR": "1", "FORCE_COLOR": "0"},
        limit=4 * 1024 * 1024,
    )
    try:
        assert proc.stdout is not None
        idle = 0
        while True:
            try:
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=IDLE_TICK)
            except asyncio.TimeoutError:
                idle += IDLE_TICK
                if idle >= IDLE_LIMIT:
                    yield {"type": "error",
                           "text": "平台通道 %d 秒没有输出，已中止。可以换个模型、把问题拆小，或者到终端里直接做"
                                   % IDLE_LIMIT}
                    break
                yield {"type": "ping"}
                continue
            idle = 0
            if not line:
                break
            s = line.decode("utf-8", "replace").strip()
            if not s:
                continue
            try:
                yield json.loads(s)
            except Exception:
                continue
    finally:
        if proc.returncode is None:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except Exception:
            pass


def _tool_label(name: str, inp: dict) -> str:
    arg = inp.get("file_path") or inp.get("path") or inp.get("pattern") or inp.get("query") or ""
    if isinstance(arg, str) and len(arg) > 60:
        arg = "…" + arg[-58:]
    return "%s %s" % (name, arg) if arg else name


async def stream(prompt: str, model: str, workspace: str, sid: str,
                 resume: bool = False):
    """产出一串事件：delta / tool / title / error / done。

    sid 为空表示调用方还没分配会话，由本函数生成并随 done 事件回传。
    """
    exe = cli_path()
    if not exe:
        yield {"type": "error", "text": "本机未安装 codebuddy CLI，请在「模型」里配置自己的 API Key"}
        return

    ws = Path(workspace or "/workspace")
    if not ws.is_dir():
        ws = Path("/workspace")

    if not sid:
        import uuid
        sid = "desk-" + uuid.uuid4().hex[:16]

    base = [exe, "-p", prompt,
            "--model", model or "auto",
            "--tools", TOOLS,
            "--permission-mode", "dontAsk",
            "--output-format", "stream-json",
            "--include-partial-messages",
            "--max-turns", "4"]
    if not TOOLS:
        # 没工具就得先把话说明白，否则模型会用文本"模拟"工具调用
        base += ["--append-system-prompt", _NO_TOOL_HINT]

    args = base + (["--resume", sid] if resume else ["--session-id", sid])

    # 首次事件
    yield {"type": "meta", "sid": sid}

    san = _Sanitizer()
    got_any = False
    dropped = 0
    retried = False
    while True:
        async for ev in _read_stream(exe, args, str(ws)):
            t = ev.get("type")

            if t == "stream_event":
                e = ev.get("event") or {}
                et = e.get("type")
                if et == "content_block_start":
                    cb = e.get("content_block") or {}
                    if cb.get("type") == "tool_use":
                        yield {"type": "tool", "text": cb.get("name", "")}
                elif et == "content_block_delta":
                    d = e.get("delta") or {}
                    if d.get("type") == "text_delta" and d.get("text"):
                        raw = d["text"]
                        txt = san.feed(raw)
                        # 注意：换行常常是单独一个 delta，不能因为 strip 为空就丢掉，
                        # 否则命令、代码块会全部黏成一行。
                        if txt:
                            if txt.strip():
                                got_any = True
                            yield {"type": "delta", "text": txt}
                        elif raw.strip():
                            dropped += 1     # 只有伪工具标记，没有正文
                    elif d.get("type") == "thinking_delta" and d.get("thinking"):
                        yield {"type": "thinking", "text": d["thinking"]}
                elif et == "message_stop":
                    pass

            elif t == "assistant":
                # 兜底：部分模型不吐 partial，只在整条消息里给全文
                msg = ev.get("message") or {}
                for blk in (msg.get("content") or []):
                    if blk.get("type") == "text" and blk.get("text") and not got_any:
                        clean = san.feed(blk["text"])
                        if clean:
                            if clean.strip():
                                got_any = True
                            yield {"type": "delta", "text": clean}
                        else:
                            dropped += 1
                    elif blk.get("type") == "tool_use":
                        yield {"type": "tool", "text": _tool_label(blk.get("name", ""), blk.get("input") or {})}

            elif t == "ai-title":
                tt = ev.get("aiTitle") or ev.get("title")
                if tt:
                    yield {"type": "title", "text": tt}

            elif t == "result":
                if ev.get("is_error") and not got_any:
                    err = ev.get("result") or ev.get("error") or "平台通道返回错误"
                    yield {"type": "error", "text": str(err)[:400]}

            elif t == "system" and ev.get("subtype") == "init":
                yield {"type": "ready", "model": ev.get("model", "")}

        # --resume 找不到会话时 CLI 会直接失败且没有任何输出 → 退化为新建
        if not got_any and resume and not retried:
            retried = True
            resume = False
            args = base + ["--session-id", sid]
            continue
        break

    yield {"type": "done", "sid": sid, "empty": not got_any, "dropped": dropped}
