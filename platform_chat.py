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
- 能动手：默认放行 Bash / Read / Glob / Grep / Write / Edit，可以在对话里
  克隆仓库、装依赖、后台起服务，再把端口报回前端（「服务卡」就此出现）

限制
----
- 只有装了 CLI 且已登录的机器能用；纯 Docker 部署默认没有，走 BYO Key
- 每次对话是一次进程冷启动（约 1~3s 首字延迟）
- 开了 Bash 就等于把「登录用户能做的事」交给对话，部署方需自行权衡；
  不想要就设 `DESK_CHAT_TOOLS=""`（纯聊天）或 `DESK_CHAT_TOOLS="Read,Glob,Grep"`（只读）
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import time
import uuid
from pathlib import Path

# 默认放行「能干活」的六个工具：对话里说"把这个项目跑起来"就能真跑起来。
# 想收紧：DESK_CHAT_TOOLS="Read,Glob,Grep"（只读）、DESK_CHAT_TOOLS=""（纯聊天）。
DEFAULT_TOOLS = "Bash,Read,Glob,Grep,Write,Edit"
TOOLS = (os.environ["DESK_CHAT_TOOLS"] if "DESK_CHAT_TOOLS" in os.environ
         else DEFAULT_TOOLS).strip()
TOOL_LIST = [t.strip() for t in TOOLS.split(",") if t.strip()]
CAN_EXEC = any(t.lower() == "bash" for t in TOOL_LIST)

# 不存在的工具会让 CLI 回一句噪声，落到聊天记录里很像故障，先滤掉
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

# 打开工具后，得把「在手机云桌面里怎么干活」讲清楚，否则模型会在前台跑
# `npm run dev` 把整轮对话卡死，或者起完服务不告诉前端端口，用户什么也看不到。
_AGENT_HINT = (
    "你运行在一台手机端的云桌面里，当前工作区就是你的 cwd，所有产物放在这里。"
    "你**有真实执行能力**（Bash / 读写文件），可以直接动手，不要只说「你可以执行…」.\n"
    "干活规矩：\n"
    "1. 用户给仓库地址就克隆到工作区下的独立目录：`git clone --depth 1 <url> <目录名>`，"
    "然后 `ls` 看清结构再决定怎么跑。网络受限时（TLS 中断、超时）**同一个网址最多试 2 次**，"
    "仍失败就换镜像：`https://cdn.jsdelivr.net/gh/<owner>/<repo>@<branch>/<文件>`、"
    "`https://codeload.github.com/<owner>/<repo>/zip/refs/heads/<branch>`，或 ghproxy 类代理。"
    "不要在同一个域名上反复重试。\n"
    "2. 起服务**必须放后台**，并且**在项目目录里起**："
    "`tmux new-session -d -s web-<端口> -c <项目绝对路径> '<启动命令>'`。"
    "关键是让 `http://127.0.0.1:<端口>/` 打开就是项目首页，而不是文件列表或 404；"
    "绝对不要在前台跑会一直占着终端的命令（npm run dev / uvicorn / flask run 都不行）。\n"
    "3. 起完必须自检：`sleep 2; curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:<端口>/`，"
    "顺手 `curl -s http://127.0.0.1:<端口>/ | head -c 200` 确认不是目录列表。"
    "不是 2xx/3xx 就去看日志找原因，别直接说成功。\n"
    "4. 依赖装不动就换最轻的路子：纯静态页用 `python3 -m http.server <端口>`，"
    "Node 项目 `npm i` 后 `npm run dev -- --host 0.0.0.0 --port <端口>`，"
    "Python 项目优先 `uvicorn`/`flask run --host 0.0.0.0`。装依赖超过 3 分钟没动静就换方案。\n"
    "5. 结束时**另起一行**写 `PORT: <端口>`（只写数字，方便前端识别）；"
    "失败就写 `FAILED: <一句话原因>`，并给一条能直接粘到终端跑的命令。\n"
    "6. 回复要短：做了什么、跑在哪个端口、下一步。不要贴大段日志。\n"
    "7. 禁止危险操作：`rm -rf /`、`mkfs`、`dd of=/dev/*`、`curl ... | sh`、改系统配置，"
    "也不要读写工作区以外的目录。\n"
)

# 认两种写法：显式的 `PORT: 3000`，以及模型顺口说的 `localhost:5173`
_PORT_RE = re.compile(
    r"(?:PORT\s*[:：]\s*|(?:https?://)?(?:localhost|127\.0\.0\.1|0\.0\.0\.0)\s*:\s*)(\d{2,5})\b",
    re.I)

_TOOL_OPEN = "<tool_call"

IDLE_TICK = 15          # 每 15 秒一次心跳
IDLE_LIMIT = 120        # 纯聊天：连续静默 120 秒判定卡死
# 带工具时不能这么急：`npm install`、`pip install` 这种几分钟不吐一个字很正常，
# 120 秒就掐断会把「正在装依赖」误判成死掉。给到 5 分钟。
IDLE_LIMIT_EXEC = 300


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


async def _read_stream(exe: str, args: list[str], cwd: str,
                       idle_limit: int = IDLE_LIMIT):
    """拉起 CLI 并逐行产出 JSON 对象。

    CLI 冷启动或长思考时可能几十秒没有任何输出，浏览器那头的 SSE 会像"卡住"；
    这里每 15 秒发一次 ping，让连接保持活跃，超过 idle_limit 真没动静才判定失败。
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
                if idle >= idle_limit:
                    yield {"type": "error",
                           "text": "平台通道 %d 秒没有输出，已中止。可以换个模型、把问题拆小，或者到终端里直接做"
                                   % idle_limit}
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


def _tool_text(c) -> str:
    """把 tool_result 的 content 掰成人能读的文本。

    CLI 给的可能是字符串，也可能是 block 列表（[{"type":"text","text":...}]）；
    直接 json.dumps 会在界面上显示成 `[{"type": "text", ...}]`，白瞎一块屏幕。
    """
    if c is None:
        return ""
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        out = []
        for b in c:
            if isinstance(b, dict):
                if b.get("type") == "text":
                    out.append(b.get("text") or "")
                elif b.get("type") == "image":
                    out.append("[图片输出]")
            elif isinstance(b, str):
                out.append(b)
        return "\n".join(x for x in out if x)
    if isinstance(c, dict):
        return c.get("text") or json.dumps(c, ensure_ascii=False)
    return str(c)


def _tail(s: str, lines: int = 6, limit: int = 600) -> str:
    """工具输出的摘要：开头几行 + 结尾两行。

    开头是「命令干了什么、结果长什么样」，结尾是「成没成、报没报错」。
    只留尾部会把 `ls` 这类「结果在开头」的输出整段切掉，只留头会把
    `Exit Code` 丢掉——所以两头都要。
    """
    txt = (s or "").replace("\r", "")
    if not txt.strip():
        return ""
    rows = [r for r in txt.split("\n") if r.strip()]
    if len(rows) <= lines:
        body = "\n".join(rows)
    else:
        # 头尾都留：开头是「命令干了什么」，结尾是「成没成」。
        # 只留尾部会把 `ls` 这种「前面才是结果」的输出切掉。
        head, tail = rows[:lines - 2], rows[-2:]
        body = "\n".join(head + ["…"] + tail)
    return body[-limit:]


def _tool_label(name: str, inp: dict) -> str:
    # Bash 的输入是 command，其它工具是路径/模式；命令可能带换行，压成一行再看
    arg = inp.get("command") or inp.get("file_path") or inp.get("path") \
        or inp.get("pattern") or inp.get("query") or ""
    if isinstance(arg, str):
        arg = " ".join(arg.split())
        if len(arg) > 64:
            arg = arg[:30] + "…" + arg[-30:]   # 两头都留：开头是命令名，结尾是目标
    return "%s %s" % (name, arg) if arg else name


async def stream(prompt: str, model: str, workspace: str, sid: str,
                 resume: bool = False):
    """产出一串事件：delta / tool / port / title / error / done。

    sid 为空表示调用方还没分配会话，由本函数生成并随 done 事件回传。
    port 事件只是「疑似端口」：调用方要自己再确认它是不是真在监听。
    """
    exe = cli_path()
    if not exe:
        yield {"type": "error", "text": "本机未安装 codebuddy CLI，请在「模型」里配置自己的 API Key"}
        return

    ws = Path(workspace or "/workspace")
    if not ws.is_dir():
        ws = Path("/workspace")

    if not sid:
        sid = "desk-" + uuid.uuid4().hex[:16]

    # 带工具时一轮要 clone → 装依赖 → 起服务 → 自检，4 轮根本不够
    max_turns = "24" if CAN_EXEC else "6"
    base = [exe, "-p", prompt,
            "--model", model or "auto",
            "--tools", TOOLS,
            "--permission-mode", "dontAsk",
            "--output-format", "stream-json",
            "--include-partial-messages",
            "--max-turns", max_turns]
    if TOOL_LIST:
        # dontAsk = 不弹窗、只按 allow 列表放行。不写进 allow，Bash 会被静默拒绝，
        # 模型只能回一句「我被权限拦住了」——这正是之前对话里"跑不起来"的原因。
        base += ["--settings", json.dumps({"permissions": {"allow": TOOL_LIST}},
                                         ensure_ascii=False)]
        base += ["--append-system-prompt", _AGENT_HINT]
    else:
        # 没工具就得先把话说明白，否则模型会用文本"模拟"工具调用
        base += ["--append-system-prompt", _NO_TOOL_HINT]

    args = base + (["--resume", sid] if resume else ["--session-id", sid])

    # 首批事件：先把「活人」信号送出去。
    # 从这一刻到模型吐第一个字，平台通道要 10~20 秒（进程冷启动 + 排队），
    # 中间没有任何正文。不发这些事件，用户看到的就只是一个转圈，
    # 分不清是在干活还是已经死了。
    yield {"type": "meta", "sid": sid}
    yield {"type": "stage", "stage": "spawn", "text": "正在唤醒模型"}

    san = _Sanitizer()
    got_any = False
    dropped = 0
    retried = False
    seen_ports: list[int] = []      # 本轮认出来的端口（去重）
    scan = ""                       # 跨 delta 的端口识别缓冲，防止标记被切两半
    idle_limit = IDLE_LIMIT_EXEC if CAN_EXEC else IDLE_LIMIT
    phase = "spawn"                 # spawn / ready / thinking / tool / writing
    t_start = time.time()
    last_visible = time.time()      # 用户侧最后一次看到新内容
    writing = False
    running: dict[str, dict] = {}   # tool_use_id -> {name, t0, detail}
    tool_labels: list[str] = []     # 本轮调用过的工具（给历史消息留个记号）

    def sniff(text: str) -> list[dict]:
        nonlocal scan
        scan = (scan + (text or ""))[-300:]
        out = []
        for m in _PORT_RE.finditer(scan):
            p = int(m.group(1))
            if 1024 <= p <= 65535 and p not in seen_ports:
                seen_ports.append(p)
                out.append({"type": "port", "port": p})
        return out

    def beat():
        """用户侧安静太久就报一次秒表。

        只靠 readline 超时不够：CLI 在跑工具时会持续吐 input_json_delta，
        那对我们的前端等于「什么都没发生」。所以每个原始事件都过一遍，
        只要屏幕上已经 8 秒没动静，就推一条 tick。
        """
        nonlocal last_visible
        if time.time() - last_visible < 8:
            return None
        last_visible = time.time()
        return {"type": "tick", "idle": int(time.time() - t_start),
                "phase": phase,
                "tool": next(iter(running.values()), {}).get("detail", "")}

    while True:
        async for ev in _read_stream(exe, args, str(ws), idle_limit):
            t = ev.get("type")
            if t in ("delta", "tool", "stage", "thinking", "ready", "port", "error"):
                last_visible = time.time()   # 屏幕上有新东西，秒表不用催
            b = beat()
            if b:
                yield b

            if t == "stream_event":
                e = ev.get("event") or {}
                et = e.get("type")
                if et == "content_block_start":
                    cb = e.get("content_block") or {}
                    if cb.get("type") == "tool_use":
                        tid = cb.get("id") or ("t%d" % (len(running) + 1))
                        nm = cb.get("name") or "tool"
                        running[tid] = {"name": nm, "t0": time.time(), "detail": ""}
                        phase = "tool"
                        yield {"type": "tool", "state": "start", "id": tid,
                               "name": nm, "seq": len(running), "phase": phase}
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
                            if not writing:
                                writing = True
                                phase = "writing"
                                yield {"type": "stage", "stage": "writing",
                                       "text": "开始作答"}
                            yield {"type": "delta", "text": txt}
                            for pev in sniff(txt):
                                yield pev
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
                            for pev in sniff(clean):
                                yield pev
                        else:
                            dropped += 1
                    elif blk.get("type") == "tool_use":
                        # 到这才拿得到完整入参，把「执行什么」补上，
                        # 前端那条「⏳ Bash」就能变成「⏳ git clone …」
                        tid = blk.get("id") or ""
                        label = _tool_label(blk.get("name", ""), blk.get("input") or {})
                        tool_labels.append(label)
                        info = running.get(tid)
                        if info is not None:
                            info["detail"] = label
                        yield {"type": "tool", "state": "detail", "id": tid,
                               "name": blk.get("name", ""), "detail": label}

            elif t == "user":
                # 工具回显里常有 "Listening on http://127.0.0.1:5173"，
                # 模型忘记写 PORT: 时靠这里捞回来
                msg = ev.get("message") or {}
                for blk in (msg.get("content") or []):
                    if isinstance(blk, dict) and blk.get("type") == "tool_result":
                        c = blk.get("content")
                        s = _tool_text(c)
                        for pev in sniff(s[:4000]):
                            yield pev
                        # 工具结束：把耗时 + 输出尾巴推给前端，
                        # 用户能看到「命令真在跑、跑出什么了」，而不是干等
                        tid = blk.get("tool_use_id") or ""
                        info = running.pop(tid, None)
                        ms = int((time.time() - info["t0"]) * 1000) if info else 0
                        phase = "thinking"
                        yield {"type": "tool", "state": "done", "id": tid,
                               "name": (info or {}).get("name", ""),
                               "detail": (info or {}).get("detail", ""),
                               "ms": ms, "ok": not blk.get("is_error"),
                               "out": _tail(s), "phase": phase}

            elif t == "ai-title":
                tt = ev.get("aiTitle") or ev.get("title")
                if tt:
                    yield {"type": "title", "text": tt}

            elif t == "result":
                if ev.get("is_error") and not got_any:
                    err = ev.get("result") or ev.get("error") or "平台通道返回错误"
                    yield {"type": "error", "text": str(err)[:400]}

            elif t == "ping":
                pass        # 保活而已，秒表由 beat() 统一发

            elif t == "system" and ev.get("subtype") == "init":
                phase = "thinking"
                yield {"type": "ready", "model": ev.get("model", "")}
                yield {"type": "stage", "stage": "thinking", "text": "模型在思考"}

        # 一轮下来只有伪工具调用、没有正文，或者续接的会话已经"学坏"了：
        # 换一个干净会话重试一次，宁可丢上下文也别让用户对着空白发呆。
        if not got_any and not retried and (dropped or resume):
            retried = True
            resume = False
            sid = "desk-" + uuid.uuid4().hex[:16]
            args = base + ["--session-id", sid]
            san = _Sanitizer()
            yield {"type": "meta", "sid": sid}
            continue
        break

    yield {"type": "done", "sid": sid, "empty": not got_any, "dropped": dropped,
           "ports": seen_ports, "tools": tool_labels}
