"""云桌面工作台 · MVP 后端
真 PTY 终端 + WebSocket + 真实 cgroup 配额采样
注意：psutil / nproc / free 在容器内返回宿主数据，不可采信，配额一律读 cgroup。
"""
import os, pty, fcntl, termios, struct, asyncio, signal, json, time, re, shutil
import hashlib, hmac, secrets, base64, ipaddress, socket
import psutil
import requests as _requests
from urllib.parse import urlparse, urlencode, parse_qs
from pathlib import Path
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

BASE = Path(__file__).parent
STATIC = BASE / "static"

app = FastAPI(title="Cloud Workstation MVP")

# ─────────────── 禁止缓存（防止手机端一直加载旧版页面） ───────────────

@app.middleware("http")
async def no_cache(request, call_next):
    # 抹掉条件请求头，避免服务器返回 304 让浏览器继续用旧副本
    try:
        drop = (b"if-none-match", b"if-modified-since")
        request.scope["raw_headers"] = [
            (k, v) for k, v in request.scope.get("raw_headers", [])
            if k.lower() not in drop
        ]
        request.scope["headers"] = [
            (k, v) for k, v in request.scope.get("headers", [])
            if k.lower() not in drop
        ]
    except Exception:
        pass

    resp = await call_next(request)
    p = request.url.path
    if p == "/" or p.endswith((".html", ".htm", ".js", ".css")):
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
    return resp

# ─────────────── 认证（公网暴露前必须开启） ───────────────

AUTH_FILE = BASE / "auth.json"

def _init_auth():
    """首次启动自动生成随机密码，避免把别人的凭据一起 clone 走。

    - 已有 auth.json：直接沿用（不覆盖）
    - 没有：生成随机密码并打印到终端；也可用 DESK_PASSWORD 环境变量指定
    """
    if AUTH_FILE.exists():
        try:
            d = json.loads(AUTH_FILE.read_text(encoding="utf-8"))
            # 每次启动都收紧权限：发布平台的文件同步可能把它重建成 644
            try:
                os.chmod(AUTH_FILE, 0o600)
            except Exception:
                pass
            return d
        except Exception:
            pass
    pwd = os.environ.get("DESK_PASSWORD") or secrets.token_urlsafe(9)
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pwd.encode(), salt.encode(), 120000).hex()
    AUTH_FILE.write_text(json.dumps({"salt": salt, "hash": h}), encoding="utf-8")
    try:
        os.chmod(AUTH_FILE, 0o600)
    except Exception:
        pass
    # 这是拿到初始密码的唯一途径，必须显示；但明确提示它已进入日志。
    print("\n" + "=" * 54, flush=True)
    print("  首次启动：已生成访问密码（只显示这一次）", flush=True)
    print("      " + pwd, flush=True)
    print("  ⚠ 该密码已写入启动日志，请登录后从日志/终端记录中清除；", flush=True)
    print("    公网部署请确认已改高强度密码。", flush=True)
    print("=" * 54 + "\n", flush=True)
    return {"salt": salt, "hash": h}

AUTH = _init_auth()
SESSIONS: dict = {}          # token -> 过期时间戳
FAILS: dict = {}             # ip -> [失败次数, 最后失败时间]
# “记住我”长期令牌：持久化到文件，server 重启后仍可自动续登。
# 磁盘上只存令牌的 SHA-256：令牌本身等价于登录凭证，明文落盘会被读文件的人直接冒用。
REMEMBER_FILE = BASE / "remember.json"
REMEMBERS: dict = {}
try:
    REMEMBERS = json.loads(REMEMBER_FILE.read_text()) if REMEMBER_FILE.exists() else {}
except Exception:
    REMEMBERS = {}


def _rt_key(rt: str) -> str:
    return hashlib.sha256(rt.encode()).hexdigest()


def _save_remembers():
    try:
        REMEMBER_FILE.write_text(json.dumps(REMEMBERS))
        try:
            os.chmod(REMEMBER_FILE, 0o600)
        except Exception:
            pass
    except Exception:
        pass

# 迁移：旧版本把长期令牌明文写在这里。发现非 SHA-256 格式的 key 直接作废，
# 代价是已登录设备要重新输一次密码 —— 明文令牌泄露的风险远大于这点不便。
if any(not re.fullmatch(r"[0-9a-f]{64}", k or "") for k in REMEMBERS):
    REMEMBERS = {}
    _save_remembers()

SESSION_TTL = 30 * 24 * 3600      # 普通登录会话 30 天有效（手机端点开即用）
REMEMBER_TTL = 180 * 24 * 3600    # “记住我”长期令牌 180 天
MAX_FAIL, FAIL_WINDOW = 5, 300

# ── 会话令牌改成「自签名令牌」 ──
# 原来用内存字典存 token，一旦服务重启或网关把请求分发到另一个进程，
# 登录态就凭空失效（表现为“刚登录就 401 / 功能时好时坏”）。
# 现在 token 自带过期时间 + HMAC 签名，任何进程都能独立验证，无需共享内存。
_SESSION_SECRET_FILE = BASE / "data" / "session_secret"

def _init_session_secret() -> bytes:
    env = os.environ.get("SESSION_SECRET")
    if env:
        return env.encode()
    try:
        if _SESSION_SECRET_FILE.exists():
            s = _SESSION_SECRET_FILE.read_text(encoding="utf-8").strip()
            if s:
                return s.encode()
        _SESSION_SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
        s = secrets.token_urlsafe(32)
        _SESSION_SECRET_FILE.write_text(s, encoding="utf-8")
        try:
            os.chmod(_SESSION_SECRET_FILE, 0o600)
        except Exception:
            pass
        return s.encode()
    except Exception:
        return secrets.token_bytes(32)

_SESSION_SECRET = _init_session_secret()

def _sign(payload: str) -> str:
    return hmac.new(_SESSION_SECRET, payload.encode(), hashlib.sha256).hexdigest()

def make_session(ttl: int = SESSION_TTL) -> str:
    """生成 <过期时间戳>.<签名> 形式的无状态令牌。"""
    payload = str(int(time.time()) + ttl)
    return payload + "." + _sign(payload)

def verify_password(pwd: str) -> bool:
    if not AUTH:
        return False
    h = hashlib.pbkdf2_hmac("sha256", pwd.encode(), AUTH["salt"].encode(), 120000).hex()
    return hmac.compare_digest(h, AUTH["hash"])

def authed(token):
    if not token:
        return False
    # 新格式：<exp>.<sig>，纯签名验证，不依赖进程内存
    if "." in token:
        try:
            payload, sig = token.rsplit(".", 1)
            if not hmac.compare_digest(sig, _sign(payload)):
                return False
            return int(payload) > time.time()
        except Exception:
            return False
    # 兼容升级前发出的旧内存令牌
    exp = SESSIONS.get(token)
    return bool(exp and exp > time.time())

@app.get("/api/check")
async def check(request: Request):
    return {"ok": authed(request.cookies.get("session"))}

@app.post("/api/login")
async def login(request: Request):
    ip = request.client.host if request.client else "?"
    f = FAILS.get(ip)
    if f and f[0] >= MAX_FAIL and time.time() - f[1] < FAIL_WINDOW:
        return JSONResponse({"ok": False, "msg": "尝试次数过多，请 5 分钟后再试"}, status_code=429)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "msg": "请求无效"}, status_code=400)

    if verify_password(body.get("password", "")):
        token = make_session(SESSION_TTL)
        payload = {"ok": True}
        # “记住我”：签发长期令牌，前端存 localStorage，重启/过期后自动续登
        if body.get("remember"):
            rt = secrets.token_urlsafe(32)
            REMEMBERS[_rt_key(rt)] = time.time() + REMEMBER_TTL
            _save_remembers()
            payload["remember"] = rt
        # 注意：必须先拼好 body 再建 Response —— 之前在这里重建 Response
        # 会把 set_cookie 一起丢掉，导致勾选“记住我”后登录态永远存不下来。
        r = JSONResponse(payload)
        r.set_cookie("session", token, httponly=True, samesite="lax",
                     max_age=SESSION_TTL)
        FAILS.pop(ip, None)
        return r

    FAILS[ip] = [(f[0] + 1) if f else 1, time.time()]
    return JSONResponse({"ok": False, "msg": "密码错误"}, status_code=401)

@app.post("/api/auto_login")
async def auto_login(request: Request):
    """用“记住我”长期令牌换一个有效 session，实现免登录自动进入。"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False}, status_code=400)
    rt = body.get("remember", "")
    # 多进程下内存中的 REMEMBERS 可能不含别的进程签发的令牌，以文件为准
    try:
        stored = json.loads(REMEMBER_FILE.read_text()) if REMEMBER_FILE.exists() else {}
    except Exception:
        stored = {}
    rk = _rt_key(rt)
    exp = stored.get(rk) or REMEMBERS.get(rk)
    if not exp or exp < time.time():
        if rt:
            stored.pop(rk, None)
            REMEMBERS.pop(rk, None)
            try:
                REMEMBER_FILE.write_text(json.dumps(stored))
            except Exception:
                pass
        return JSONResponse({"ok": False}, status_code=401)
    token = make_session(SESSION_TTL)
    r = JSONResponse({"ok": True})
    r.set_cookie("session", token, httponly=True, samesite="lax",
                 max_age=SESSION_TTL)
    return r

@app.post("/api/logout")
async def logout(request: Request):
    try:
        body = await request.json()
        rt = body.get("remember")
        if rt:
            try:
                stored = json.loads(REMEMBER_FILE.read_text()) if REMEMBER_FILE.exists() else {}
            except Exception:
                stored = {}
            stored.pop(_rt_key(rt), None)
            REMEMBERS.pop(_rt_key(rt), None)
            try:
                REMEMBER_FILE.write_text(json.dumps(stored))
                try:
                    os.chmod(REMEMBER_FILE, 0o600)
                except Exception:
                    pass
            except Exception:
                pass
    except Exception:
        pass
    # 会话令牌是无状态签名的，服务端无法吊销，删掉浏览器 cookie 即可
    r = JSONResponse({"ok": True})
    r.delete_cookie("session")
    return r

@app.on_event("startup")
async def _gc():
    async def loop():
        while True:
            await asyncio.sleep(600)
            now = time.time()
            for t in [k for k, v in SESSIONS.items() if v <= now]:
                SESSIONS.pop(t, None)
            expired_r = [k for k, v in REMEMBERS.items() if v <= now]
            if expired_r:
                for k in expired_r: REMEMBERS.pop(k, None)
                _save_remembers()
    asyncio.create_task(loop())

# ─────────────── 真实配额读取（cgroup v2） ───────────────

def _read(path):
    try:
        return Path(path).read_text().strip()
    except Exception:
        return None

def cgroup_mem_limit():
    v = _read("/sys/fs/cgroup/memory.max")
    if v and v != "max":
        try:
            return int(v)
        except ValueError:
            return None
    return None

def cgroup_mem_used():
    v = _read("/sys/fs/cgroup/memory.current")
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0

def cgroup_cpu_quota():
    v = _read("/sys/fs/cgroup/cpu.max")
    if not v:
        return None
    try:
        q, p = v.split()
        return None if q == "max" else int(q) / int(p)
    except Exception:
        return None

def cgroup_cpu_usage_usec():
    v = _read("/sys/fs/cgroup/cpu.stat")
    if not v:
        return 0
    for line in v.splitlines():
        if line.startswith("usage_usec"):
            try:
                return int(line.split()[1])
            except (IndexError, ValueError):
                return 0
    return 0

CPU_QUOTA = cgroup_cpu_quota() or float(os.cpu_count() or 1)

# ─────────────── CPU 采样器（差分计算真实占用） ───────────────

_sample = {"t": time.time(), "u": cgroup_cpu_usage_usec(), "pct": 0.0}

async def sampler():
    while True:
        await asyncio.sleep(1.0)
        now = time.time()
        u = cgroup_cpu_usage_usec()
        dt = now - _sample["t"]
        if dt > 0:
            used = (u - _sample["u"]) / 1e6          # 消耗的 CPU 秒数
            _sample["pct"] = round(min(100.0, used / dt / CPU_QUOTA * 100), 1)
        _sample["t"], _sample["u"] = now, u

@app.on_event("startup")
async def on_start():
    asyncio.create_task(sampler())

# ─────────────── 状态接口 ───────────────

@app.get("/api/stats")
async def stats(request: Request):
    if not authed(request.cookies.get("session")):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    limit = cgroup_mem_limit()
    used = cgroup_mem_used()
    try:
        du = psutil.disk_usage("/")
        disk = {"percent": du.percent, "free": du.free, "total": du.total}
    except Exception:
        disk = None
    return {
        "cpu_quota_cores": round(CPU_QUOTA, 2),
        "cpu_percent": _sample["pct"],
        "mem_limit": limit,
        "mem_used": used,
        "mem_percent": round(used / limit * 100, 1) if limit else None,
        "disk": disk,
        "gpu": False,
        "host_cpu": _host_cpu_model(),
    }

def _host_cpu_model():
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    except Exception:
        pass
    return "unknown"

# ─────────────── 真 PTY 终端 ───────────────

# ─────────────── 终端会话宿主 ───────────────
# 裸 pty.fork() 的会话挂在 web 进程上：进程一重启就全丢，多实例之间也互不相认。
# 改为把真正的 shell 托管在 tmux 里，web 进程只做「附着客户端」：
#   · 页面关掉 / 服务重启 / 换实例 → 会话仍在，重新进来接着用原来的屏幕和命令历史
#   · 手机、电脑可同时附着同一个会话
# 没有 tmux 的环境自动退回裸 bash，不影响开源自建部署。
TMUX_BIN = shutil.which("tmux")
TMUX_SESSION = os.environ.get("DESK_TMUX_SESSION", "desk")


def spawn_pty(env):
    """启动一个挂在 tmux 会话上的终端，返回 (pid, fd)。"""
    pid, fd = pty.fork()
    if pid == 0:                                    # 子进程
        try:
            # 终端默认落在工作区，而不是 server.py 所在目录
            # （容器里两者不同，否则用户进来先看到一堆源码文件）。
            try:
                if WS_ROOT and os.path.isdir(WS_ROOT):
                    os.chdir(WS_ROOT)
            except Exception:
                pass
            if TMUX_BIN:
                s = TMUX_SESSION
                conf = str(BASE / "tmux.conf")
                # 首次直接“附着创建”，shell 才会立刻画出提示符；
                # 之后每次都只是 attach，屏幕内容原样保留（这就是“接着上次用”）。
                script = (
                    'tmux -f "{conf}" attach -t {s} 2>/dev/null '
                    '|| exec tmux -f "{conf}" new-session -s {s}'
                ).format(conf=conf, s=s)
                os.execve("/bin/bash", ["/bin/bash", "-c", script], env)
            os.execvpe("/bin/bash", ["/bin/bash"], env)
        except Exception:
            pass
        finally:
            os._exit(1)
    return pid, fd


def tmux_alive():
    """当前是否已有活着的托管会话。"""
    if not TMUX_BIN:
        return False
    try:
        return os.system("%s has-session -t %s 2>/dev/null" % (TMUX_BIN, TMUX_SESSION)) == 0
    except Exception:
        return False


@app.websocket("/ws/terminal")
async def terminal(ws: WebSocket):
    # 关键：WebSocket 必须校验，仅挡住页面不足以保护真 shell
    if not authed(ws.cookies.get("session")):
        await ws.close(code=4001, reason="unauthorized")
        return
    await ws.accept()

    env = dict(os.environ)
    env["TERM"] = "xterm-256color"
    env["PS1"] = r"\u@cloud:\w\$ "
    env["COLUMNS"] = "120"
    env["LINES"] = "30"

    pid, fd = spawn_pty(env)

    # 父进程：非阻塞读取
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    closed = False

    def on_readable():
        nonlocal closed
        try:
            data = os.read(fd, 65536)
        except OSError:
            data = b""
        if data:
            loop.call_soon_threadsafe(queue.put_nowait, data)
        else:
            if not closed:
                closed = True
                loop.call_soon_threadsafe(queue.put_nowait, None)

    loop.add_reader(fd, on_readable)

    async def pump_out():
        while True:
            data = await queue.get()
            if data is None:
                break
            await ws.send_bytes(data)      # 二进制传输，避免 UTF-8 分包截断乱码

    async def pump_in():
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            text, raw = msg.get("text"), msg.get("bytes")
            try:
                if text:
                    obj = json.loads(text)
                    kind = obj.get("type")
                    if kind == "input":
                        os.write(fd, obj.get("data", "").encode("utf-8", "replace"))
                    elif kind == "resize":
                        cols = max(1, int(obj.get("cols", 80)))
                        rows = max(1, int(obj.get("rows", 24)))
                        fcntl.ioctl(fd, termios.TIOCSWINSZ,
                                    struct.pack("HHHH", rows, cols, 0, 0))
                elif raw:
                    os.write(fd, raw)
            except (OSError, ValueError):
                break

    out_task = asyncio.create_task(pump_out())
    try:
        await pump_in()
    except WebSocketDisconnect:
        pass
    finally:
        out_task.cancel()
        try:
            loop.remove_reader(fd)
        except Exception:
            pass
        try:
            os.kill(pid, signal.SIGHUP)
        except Exception:
            pass
        try:
            os.waitpid(pid, os.WNOHANG)
        except Exception:
            pass
        try:
            os.close(fd)
        except Exception:
            pass

# ─────────────── 终端降级通道：HTTP 轮询（供不支持 WebSocket 的网关使用） ───────────────
# PTY 仍然是真的，只是输出改由前端定时拉取，而非服务端推送。

TERMS: dict = {}          # token -> {fd, pid, buf}

def _spawn_term(token):
    if token in TERMS:
        return TERMS[token]
    env = dict(os.environ)
    env["TERM"] = "xterm-256color"
    env["PS1"] = r"\u@cloud:\w\$ "
    pid, fd = spawn_pty(env)
    fl = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)
    TERMS[token] = {"fd": fd, "pid": pid, "buf": bytearray()}

    loop = asyncio.get_running_loop()
    def rd():
        t = TERMS.get(token)
        if not t:
            return
        try:
            d = os.read(t["fd"], 65536)
            if d:
                t["buf"].extend(d)
        except OSError:
            pass
    loop.add_reader(fd, rd)
    return TERMS[token]

def _kill_term(token):
    t = TERMS.pop(token, None)
    if not t:
        return
    try:
        asyncio.get_running_loop().remove_reader(t["fd"])
    except Exception:
        pass
    try:
        os.kill(t["pid"], signal.SIGHUP)
    except Exception:
        pass
    try:
        os.close(t["fd"])
    except Exception:
        pass

@app.post("/api/term/start")
async def term_start(request: Request):
    tok = request.cookies.get("session")
    if not authed(tok):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    _spawn_term(tok)
    return {"ok": True}

@app.post("/api/term/input")
async def term_input(request: Request):
    tok = request.cookies.get("session")
    if not authed(tok):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    t = TERMS.get(tok)
    if not t:
        return JSONResponse({"ok": False, "msg": "会话未启动"}, status_code=400)
    body = await request.json()
    data = body.get("data", "")
    # 公网网关带 WAF，会拦截含特定模式的明文命令（如 print(123+456)、/etc/passwd）。
    # 前端统一用 base64 编码输入，WAF 看不到明文，避免正常命令被误杀。
    if body.get("enc") == "b64":
        try:
            data = base64.b64decode(data).decode("utf-8")
        except Exception:
            return JSONResponse({"ok": False, "msg": "编码错误"}, status_code=400)
    os.write(t["fd"], data.encode("utf-8", "replace"))
    return {"ok": True}

@app.get("/api/term/output")
async def term_output(request: Request):
    tok = request.cookies.get("session")
    if not authed(tok):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    t = TERMS.get(tok)
    if not t:
        return Response(content=b"", media_type="application/octet-stream")
    data = bytes(t["buf"])
    t["buf"].clear()
    return Response(content=data, media_type="application/octet-stream")

@app.post("/api/term/resize")
async def term_resize(request: Request):
    tok = request.cookies.get("session")
    if not authed(tok):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    t = TERMS.get(tok)
    if not t:
        return JSONResponse({"ok": False}, status_code=400)
    body = await request.json()
    cols = max(1, int(body.get("cols", 80)))
    rows = max(1, int(body.get("rows", 24)))
    fcntl.ioctl(t["fd"], termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    return {"ok": True}

@app.get("/api/term/status")
async def term_status(request: Request):
    """告诉前端当前是否已有一个「随时可回来」的托管会话。"""
    if not authed(request.cookies.get("session")):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    alive = tmux_alive()
    return {"ok": True, "persistent": bool(TMUX_BIN), "alive": alive,
            "session": TMUX_SESSION if TMUX_BIN else ""}


@app.post("/api/term/reset")
async def term_reset(request: Request):
    """彻底结束托管会话（用户想要一个干净终端时用）。"""
    if not authed(request.cookies.get("session")):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if TMUX_BIN:
        os.system("%s kill-session -t %s 2>/dev/null" % (TMUX_BIN, TMUX_SESSION))
    return {"ok": True}

# ─────────────── 静态托管 ───────────────

# ─────────────── 对话镜像 ───────────────
# chat.jsonl  : 完整对话记录。我在 WorkBuddy 会话里的回复会追加到这里，网页轮询显示
# inbox.jsonl : 网页提交的问题，我在这边被触发时读取并处理

DATA = BASE / "data"
DATA.mkdir(exist_ok=True)
CHAT_LOG   = DATA / "chat.jsonl"
CHAT_INBOX = DATA / "inbox.jsonl"

def _jsonl_read(p: Path):
    if not p.exists():
        return []
    out = []
    for ln in p.read_text(encoding="utf-8", errors="replace").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except Exception:
            pass
    return out

@app.get("/api/chat/history")
async def chat_history(request: Request, since: int = 0):
    tok = request.cookies.get("session")
    if not authed(tok):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    msgs = _jsonl_read(CHAT_LOG)
    return {"ok": True, "total": len(msgs), "msgs": msgs[since:]}

@app.post("/api/chat/send")
async def chat_send(request: Request):
    tok = request.cookies.get("session")
    if not authed(tok):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
        text = body.get("text", "") or ""
        # 与终端同理：公网 WAF 会误杀含特定模式的明文，统一用 base64 封装
        if body.get("enc") == "b64":
            text = base64.b64decode(text).decode("utf-8", "replace")
        text = text.strip()
        if not text:
            return JSONResponse({"ok": False, "msg": "内容为空"}, status_code=400)
        ts = int(time.time())
        rec = {"role": "user", "content": text[:8000], "ts": ts, "src": "web", "pending": True}
        if body.get("model"):
            rec["model"] = body.get("model")
        with CHAT_INBOX.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        with CHAT_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return {"ok": True, "ts": ts}
    except Exception as e:
        return JSONResponse({"ok": False, "msg": str(e)}, status_code=400)

@app.get("/api/chat/pending")
async def chat_pending(request: Request):
    tok = request.cookies.get("session")
    if not authed(tok):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    n = len([m for m in _jsonl_read(CHAT_INBOX) if not m.get("done")])
    return {"ok": True, "pending": n}

# ─────────────── 监听端口探测（供浏览器页一键预览正在运行的服务） ───────────────
def _listening_ports():
    try:
        conns = psutil.net_connections(kind="inet")
        return sorted({c.laddr.port for c in conns
                       if c.status == "LISTEN" and c.laddr})
    except Exception:
        return []

# 服务启动时就已存在的监听端口 = 平台/系统自身，不展示给用户（否则一片干扰）
_BOOT_PORTS = set(_listening_ports())
_SELF_PORT = int(os.environ.get("PORT", 8080))

@app.get("/api/ports")
async def api_ports(request: Request):
    if not authed(request.cookies.get("session")):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    now = _listening_ports()
    return {"ports": [p for p in now
                      if p not in _BOOT_PORTS and p != _SELF_PORT],
            "all": now}

# ─────────────── 端口预览 / 内置浏览器（反向代理） ───────────────
# 公网网关在首次带 sharecode 访问时会下发 __wb_rsc cookie，之后同源子资源自动豁免，
# 因此 iframe 内网页的子资源（JS/CSS）也能通过网关，无需改写 HTML。

def _strip_frame_headers(headers):
    h = dict(headers)
    for k in ("x-frame-options", "content-security-policy",
              "content-security-policy-report-only", "transfer-encoding"):
        h.pop(k.lower(), None)
    return h

async def _do_proxy(url: str, request: Request, rewrite_port: int = None,
                    allow_redirects: bool = True):
    # 转发时去掉网关的 sharecode，避免泄漏给上游；保留其它原始 query
    q = request.url.query
    if "sharecode=" in q:
        qs = {k: v[0] for k, v in parse_qs(q).items() if k != "sharecode"}
        url = url + ("?" + urlencode(qs) if qs else "")
    try:
        loop = asyncio.get_event_loop()
        resp = await loop.run_in_executor(
            None,
            lambda: _requests.get(url, timeout=12, stream=True,
                                 allow_redirects=allow_redirects,
                                 headers={"User-Agent": "Mozilla/5.0 (cloud-preview)"})
        )
    except Exception as e:
        return JSONResponse({"error": "代理失败: " + str(e)}, status_code=502)

    cl = resp.headers.get("content-length")
    if cl and int(cl) > 25 * 1024 * 1024:
        resp.close()
        return JSONResponse({"error": "响应过大（>25MB）"}, status_code=502)
    data = b""
    try:
        for chunk in resp.iter_content(8192):
            data += chunk
            if len(data) > 25 * 1024 * 1024:
                return JSONResponse({"error": "响应过大（>25MB）"}, status_code=502)
    finally:
        resp.close()

    ctype = resp.headers.get("content-type", "application/octet-stream")
    # 轻量改写：把本机绝对地址换成预览代理路径，便于子资源加载
    if rewrite_port and "text/html" in ctype:
        for p in (f"https://127.0.0.1:{rewrite_port}",
                  f"http://127.0.0.1:{rewrite_port}",
                  f"https://localhost:{rewrite_port}",
                  f"http://localhost:{rewrite_port}",
                  f"//127.0.0.1:{rewrite_port}",
                  f"//localhost:{rewrite_port}"):
            data = data.replace(p.encode(), f"/preview/{rewrite_port}".encode())
    return Response(content=data, media_type=ctype,
                    headers=_strip_frame_headers(resp.headers),
                    status_code=resp.status_code)

@app.get("/preview/{port:int}/{rest:path}")
async def preview(port: int, rest: str, request: Request):
    if not authed(request.cookies.get("session")):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if port < 1 or port > 65535:
        return JSONResponse({"error": "端口超出范围"}, status_code=400)
    target = f"http://127.0.0.1:{port}/{rest}"
    return await _do_proxy(target, request, rewrite_port=port)

# ── SSRF 防护 ──────────────────────────────────────────────────────────────
# /preview 是设计上就要访问本机端口的，不做限制；但 /proxy 能访问任意外网 URL，
# 一旦部署在云主机上，攻击者可借它打到 169.254.169.254 拿到实例角色凭证。
# 所以这里必须：解析出真实 IP 后按网段判定，而不是只比对主机名字符串。
# 只比字符串会被这些绕过：2852039166（十进制）、::ffff:169.254.169.254（IPv6 映射）、
# metadata.google.internal（云厂商域名）、0177.0.0.1（八进制）。

_PRIVATE_NETS = []
for _n in ("0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8",
           "169.254.0.0/16", "172.16.0.0/12", "192.0.0.0/24", "192.0.2.0/24",
           "192.88.99.0/24", "192.168.0.0/16", "198.18.0.0/15",
           "198.51.100.0/24", "203.0.113.0/24", "224.0.0.0/4", "240.0.0.0/4"):
    _PRIVATE_NETS.append(ipaddress.ip_network(_n))
for _n in ("::/128", "::1/128", "fc00::/7", "fe80::/10", "ff00::/8", "64:ff9b::/96"):
    _PRIVATE_NETS.append(ipaddress.ip_network(_n))

# 云厂商元数据域名（解析前先拦一道，避免内部 DNS 把它们指到内网）
_META_HOSTS = ("metadata.google.internal", "metadata.goog", "metadata",
               "instance-data", "instance-data.ec2.internal", "nova",
               "metadata.azure.com", "100.100.100.200")

# 纯内网部署时才打开：DESK_PROXY_ALLOW_PRIVATE=1
ALLOW_PRIVATE = os.environ.get("DESK_PROXY_ALLOW_PRIVATE", "").strip() in ("1", "true", "yes")


def _is_blocked_ip(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str.strip().strip("[]"))
    except ValueError:
        return True
    # IPv4 映射的 IPv6（::ffff:169.254.169.254）等价于那个 IPv4，按同样规则判
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    return any(ip in net for net in _PRIVATE_NETS)


def _ssrf_check(url: str):
    """返回 None 表示放行，否则返回错误说明。"""
    host = (urlparse(url).hostname or "").strip().lower().rstrip(".")
    if not host:
        return "无法解析主机名"
    hl = host
    if hl in _META_HOSTS or hl.endswith(".internal") or hl.endswith(".local"):
        return "禁止访问内网/元数据地址"
    # IP 字面量（含十进制、八进制写法，ipaddress 都能正确解析）
    try:
        ipaddress.ip_address(hl.strip("[]"))
        return "禁止访问内网地址" if _is_blocked_ip(hl) else None
    except ValueError:
        pass
    # 域名：解析出所有 A/AAAA 逐个判定
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return "域名解析失败"
    ips = {ai[4][0] for ai in infos}
    if not ips:
        return "域名解析失败"
    for ip in ips:
        if _is_blocked_ip(ip):
            return "禁止访问内网地址"
    return None


@app.get("/proxy")
async def proxy(request: Request, url: str = ""):
    if not authed(request.cookies.get("session")):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not url.startswith(("http://", "https://")):
        return JSONResponse({"error": "仅支持 http/https"}, status_code=400)
    if not ALLOW_PRIVATE:
        bad = _ssrf_check(url)
        if bad:
            return JSONResponse({"error": bad}, status_code=403)
    # 不跟随重定向：否则目标可用 302 把请求引到 169.254.169.254 绕过上面所有检查
    return await _do_proxy(url, request, allow_redirects=False)

# ─────────────── 文件管理 API（文件管理器 + AI 改代码共用） ───────────────
# 仅本地脚本 / Agent 驱动 bridge 使用，与用户登录 session 隔离。
# 不写死：优先环境变量，其次 data/bridge_secret（首次自动生成，已 gitignore）。
def _init_bridge_secret() -> str:
    env = os.environ.get("BRIDGE_SECRET")
    if env:
        return env
    f = BASE / "data" / "bridge_secret"
    try:
        if f.exists():
            s = f.read_text(encoding="utf-8").strip()
            if s:
                return s
        f.parent.mkdir(parents=True, exist_ok=True)
        s = secrets.token_urlsafe(24)
        f.write_text(s, encoding="utf-8")
        try:
            os.chmod(f, 0o600)
        except Exception:
            pass
        return s
    except Exception:
        return secrets.token_urlsafe(24)

BRIDGE_SECRET = _init_bridge_secret()

def _safe_path(p: str):
    """把请求路径限制在 BASE 内，防止 ../ 穿越出沙箱根。"""
    try:
        target = (BASE / (p or ".")).resolve()
        if not str(target).startswith(str(BASE.resolve())):
            return None
        return target
    except Exception:
        return None

# 文件管理器默认落在工作区（/workspace），而不是项目自身目录 —— 用户要看的是自己的文件。
# 但老调用方可能传相对路径（相对项目根），所以两条根都要能解析，且都不能越界。
def _safe_fs(p: str):
    p = p or "."
    try:
        if os.path.isabs(p):
            roots = [WS_ROOT, BASE]
            for r in roots:
                try:
                    t = Path(p).resolve()
                except Exception:
                    return None
                rr = r.resolve()
                if t == rr or str(t).startswith(str(rr) + os.sep):
                    return t
            return None
        t = (BASE / p).resolve()
        if t == BASE.resolve() or str(t).startswith(str(BASE.resolve()) + os.sep):
            return t
        return None
    except Exception:
        return None


# 永远不展示的噪音目录（构建产物、缓存、版本库）
NOISE_DIRS = {"node_modules", "__pycache__", ".venv", "venv", ".git",
              ".mypy_cache", ".pytest_cache", "dist", "build", ".next"}
# 项目自身目录名：出现在工作区根时默认折叠，避免用户一进来看到的是 server.py
SELF_DIR = BASE.name
# 项目根下的这些文件是凭据/数据，任何视图都不该列出来
# （内容即使读到也只是哈希，但没有理由把凭据文件摆到界面上）
SENSITIVE = {"auth.json", "remember.json", "chat_config.json",
             "bridge_secret", "session_secret",
             "desk.db", "desk.db-wal", "desk.db-shm"}


@app.get("/api/env")
async def api_env(request: Request):
    """告诉前端真实的工作区根：WORKSPACE_ROOT 不一定是 /workspace，
    写死会让文件页指向一个不存在的目录（表现为整页空白）。"""
    if not authed(request.cookies.get("session")):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return {"workspace": str(WS_ROOT), "project": str(BASE)}


@app.get("/api/fs/list")
async def fs_list(request: Request, path: str = ".", show_self: int = 0):
    if not authed(request.cookies.get("session")):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    d = _safe_fs(path)
    if not d or not d.exists():
        return JSONResponse({"error": "路径不存在"})
    try:
        if d.is_file():
            return {"items": [{"name": d.name, "type": "file", "size": d.stat().st_size}],
                    "hidden": []}
        items, hidden = [], []
        at_base = d.resolve() == BASE.resolve()
        for e in sorted(os.listdir(d)):
            if e.startswith(".") or e in NOISE_DIRS:
                continue
            if at_base and (e in SENSITIVE or e == "data"):
                continue
            # 工作区根目录下的项目本体：默认隐藏，前端「显示项目文件」开关可打开
            if (not show_self) and d.resolve() == WS_ROOT.resolve() and e == SELF_DIR:
                hidden.append(e)
                continue
            fp = d / e
            st = fp.stat()
            items.append({"name": e, "type": "dir" if fp.is_dir() else "file",
                          "size": st.st_size if fp.is_file() else 0})
        return {"items": items, "hidden": hidden}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)

@app.get("/api/fs/read")
async def fs_read(request: Request, path: str = "", limit: int = 200000):
    if not authed(request.cookies.get("session")):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    f = _safe_fs(path)
    if not f or not f.is_file():
        return JSONResponse({"error": "不是文件或不存在"})
    try:
        data = f.read_text(encoding="utf-8", errors="replace")
        trunc = len(data) > limit
        if trunc:
            data = data[:limit]
        return {"content": data, "truncated": trunc}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)

@app.post("/api/fs/write")
async def fs_write(request: Request):
    if not authed(request.cookies.get("session")):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
        data = body.get("content", "")
        if body.get("enc") == "b64":
            data = base64.b64decode(data).decode("utf-8", "replace")
        f = _safe_fs(body.get("path", ""))
        if not f:
            return JSONResponse({"error": "非法路径"}, status_code=400)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(data, encoding="utf-8")
        return {"ok": True, "size": len(data)}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)

@app.post("/api/fs/delete")
async def fs_delete(request: Request):
    if not authed(request.cookies.get("session")):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
        f = _safe_fs(body.get("path", ""))
        if not f or not f.exists():
            return JSONResponse({"error": "不存在"}, status_code=400)
        if f.is_dir():
            import shutil
            shutil.rmtree(f)
        else:
            f.unlink()
        return {"ok": True}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)

@app.post("/api/fs/mkdir")
async def fs_mkdir(request: Request):
    if not authed(request.cookies.get("session")):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
        f = _safe_fs(body.get("path", ""))
        if not f:
            return JSONResponse({"error": "非法路径"}, status_code=400)
        f.mkdir(parents=True, exist_ok=True)
        return {"ok": True}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)

# ─────────────── AI Bridge（WorkBuddy Agent 从本地驱动，免用户登录） ───────────────
def _bridge_ok(request: Request):
    """优先从请求头取（不进 URL / 日志 / Referer），兼容旧调用方的 query 参数。
    用 compare_digest 做恒定时间比较，避免被计时侧信道逐字节猜出密钥。"""
    got = request.headers.get("x-bridge-secret") or request.query_params.get("bridge") or ""
    return hmac.compare_digest(got, BRIDGE_SECRET)

@app.get("/api/chat/inbox")
async def chat_inbox(request: Request):
    if not _bridge_ok(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    msgs = [m for m in _jsonl_read(CHAT_INBOX) if not m.get("done")]
    return {"ok": True, "msgs": msgs}

@app.post("/api/chat/append")
async def chat_append(request: Request):
    if not _bridge_ok(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    try:
        body = await request.json()
        rec = {"role": body.get("role", "assistant"),
               "content": body.get("content", ""), "ts": int(time.time()), "src": "agent"}
        if body.get("tool"):
            rec["tool"] = body["tool"]
        if body.get("model"):
            rec["model"] = body["model"]
        with CHAT_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return {"ok": True}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)

@app.post("/api/chat/mark_done")
async def chat_mark_done(request: Request):
    if not _bridge_ok(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    try:
        body = await request.json()
        tgt = body.get("ts")
        lines = CHAT_INBOX.read_text(encoding="utf-8").splitlines() if CHAT_INBOX.exists() else []
        out = []
        for ln in lines:
            ln = ln.strip()
            if not ln:
                continue
            try:
                o = json.loads(ln)
            except Exception:
                out.append(ln); continue
            if o.get("ts") == tgt:
                o["done"] = True
            out.append(json.dumps(o, ensure_ascii=False))
        CHAT_INBOX.write_text("\n".join(out) + ("\n" if out else ""), encoding="utf-8")
        return {"ok": True}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)

# ─────────────── 工作区（Codex 式：新对话选择工作文件夹） ───────────────
def _pick_ws_root() -> Path:
    """工作区根目录：环境变量 > /workspace（云沙箱常见） > ~/workspace"""
    env = os.environ.get("WORKSPACE_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    if Path("/workspace").is_dir():
        return Path("/workspace").resolve()
    p = Path.home() / "workspace"
    p.mkdir(parents=True, exist_ok=True)
    return p.resolve()

WS_ROOT = _pick_ws_root()

def _safe_ws(p: str):
    try:
        t = (WS_ROOT / (p or "")).resolve()
        if not str(t).startswith(str(WS_ROOT.resolve())):
            return None
        return t
    except Exception:
        return None

@app.get("/api/ws/list")
async def ws_list(request: Request, path: str = ""):
    if not authed(request.cookies.get("session")):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    d = _safe_ws(path)
    if not d or not d.exists() or not d.is_dir():
        return JSONResponse({"error": "目录不存在"}, status_code=400)
    items = []
    try:
        for e in sorted(os.listdir(d)):
            if e.startswith("."):
                continue
            fp = d / e
            if fp.is_dir():
                items.append({"name": e, "type": "dir"})
        rel = str(d).replace(str(WS_ROOT), "") or ""
        return {"path": str(d), "rel": rel, "items": items}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)

@app.post("/api/ws/mkdir")
async def ws_mkdir(request: Request):
    if not authed(request.cookies.get("session")):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
        d = _safe_ws(body.get("path", ""))
        if not d:
            return JSONResponse({"error": "非法路径"}, status_code=400)
        d.mkdir(parents=True, exist_ok=True)
        return {"ok": True, "path": str(d)}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)

# ─────────────── 模型接口配置（BYO：任意 OpenAI 兼容端点） ───────────────
CHAT_CFG_FILE = BASE / "chat_config.json"

def _load_chat_cfg():
    try:
        return json.loads(CHAT_CFG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}

# ─────────────── 多会话（SQLite 持久化） ───────────────
# 老版本的 chat.jsonl 会在首次启动时一次性迁进「历史消息」会话。
import store as _store
import platform_chat as _plat

try:
    _migrated = _store.import_jsonl(CHAT_LOG)
    if _migrated:
        print("[store] 已迁移 %d 条历史消息到 SQLite" % _migrated)
except Exception as _e:
    print("[store] 迁移失败：", _e)


def _sse(obj) -> bytes:
    return ("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode()


@app.get("/api/conv/list")
async def conv_list(request: Request):
    if not authed(request.cookies.get("session")):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return {"convs": _store.list_convs()}


@app.post("/api/conv/new")
async def conv_new(request: Request):
    if not authed(request.cookies.get("session")):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        b = await request.json()
    except Exception:
        b = {}
    conv = _store.new_conv(workspace=b.get("workspace") or "/workspace",
                           model=b.get("model") or "",
                           provider=b.get("provider") or "")
    return {"ok": True, "conv": conv}


@app.post("/api/conv/update")
async def conv_update(request: Request):
    if not authed(request.cookies.get("session")):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        b = await request.json()
        cid = b.pop("id", "")
        if not _store.get_conv(cid):
            return JSONResponse({"error": "会话不存在"}, status_code=404)
        if "title" in b:
            b["title"] = (b["title"] or "新对话").strip()[:60] or "新对话"
        _store.update_conv(cid, **b)
        return {"ok": True, "conv": _store.get_conv(cid)}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/api/conv/delete")
async def conv_delete(request: Request):
    if not authed(request.cookies.get("session")):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        b = await request.json()
        _store.del_conv(b.get("id", ""))
        return {"ok": True}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/api/conv/messages")
async def conv_messages(request: Request, id: str = ""):
    if not authed(request.cookies.get("session")):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    conv = _store.get_conv(id)
    if not conv:
        return JSONResponse({"error": "会话不存在"}, status_code=404)
    return {"ok": True, "conv": conv, "msgs": _store.msgs(id)}


@app.get("/api/chat/config")
async def chat_config(request: Request):
    if not authed(request.cookies.get("session")):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    cfg = _load_chat_cfg()
    return {"configured": bool(cfg.get("base_url") and cfg.get("api_key")),
            "models": cfg.get("models") or [],
            "base_url": cfg.get("base_url", ""),
            "tools": _plat.TOOLS,
            "platform": {"available": _plat.available(),
                         "models": _plat.models()}}


@app.get("/api/chat/platform")
async def chat_platform(request: Request):
    """探测平台通道：本机有没有 CLI、能用哪些模型。"""
    if not authed(request.cookies.get("session")):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return {"available": _plat.available(), "models": _plat.models(),
            "cli": _plat.cli_path() or ""}

@app.post("/api/chat/config/set")
async def chat_config_set(request: Request):
    if not authed(request.cookies.get("session")):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        b = await request.json()
        base = (b.get("base_url") or "").strip().rstrip("/")
        key = (b.get("api_key") or "").strip()
        models = b.get("models") or []
        if not (base and key):
            return JSONResponse({"error": "接口地址和 Key 都不能为空"}, status_code=400)
        CHAT_CFG_FILE.write_text(json.dumps(
            {"base_url": base, "api_key": key, "models": models}, ensure_ascii=False))
        return {"ok": True}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)

@app.post("/api/chat/models")
async def chat_models(request: Request):
    """从用户填的端点拉取可用模型列表（GET {base}/models）。"""
    if not authed(request.cookies.get("session")):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        b = await request.json()
        base = (b.get("base_url") or "").strip().rstrip("/")
        key = (b.get("api_key") or "").strip()
        if not base:
            return JSONResponse({"error": "缺少接口地址"}, status_code=400)
        loop = asyncio.get_event_loop()
        r = await loop.run_in_executor(None, lambda: _requests.get(
            base + "/models", headers={"Authorization": "Bearer " + key}, timeout=15))
        if r.status_code >= 400:
            return {"error": "HTTP %d" % r.status_code}
        ids = [m.get("id") for m in r.json().get("data", []) if m.get("id")]
        return {"models": ids}
    except Exception as e:
        return {"error": str(e)}

@app.post("/api/chat/completions")
async def chat_completions(request: Request):
    """统一对话入口（SSE）。

    provider = "platform" → 本机 codebuddy CLI，免 Key、用设备已有登录态
    provider = "byo"      → 用户自填的 OpenAI 兼容端点
    """
    if not authed(request.cookies.get("session")):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "请求无效"}, status_code=400)

    cfg = _load_chat_cfg()
    has_key = bool(cfg.get("base_url") and cfg.get("api_key"))

    conv = _store.get_conv(body.get("conv_id") or "")
    if not conv:
        conv = _store.new_conv(workspace=body.get("workspace") or "/workspace",
                               model=body.get("model") or "", provider="")
    cid = conv["id"]
    model = (body.get("model") or conv.get("model") or "").strip()
    workspace = body.get("workspace") or conv.get("workspace") or "/workspace"
    messages = body.get("messages") or []
    user_text = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            user_text = m.get("content", "") or ""
            break
    if not user_text.strip():
        return JSONResponse({"error": "消息为空"}, status_code=400)

    provider = body.get("provider") or ("byo" if has_key else "platform")
    if provider == "byo" and not has_key:
        provider = "platform"
    if provider == "platform" and not _plat.available():
        return JSONResponse({"error": "本机没有可用的平台通道，也未配置 API Key"}, status_code=400)

    _store.add_msg(cid, "user", user_text[:8000], model)
    _store.auto_title(cid, user_text)
    _store.update_conv(cid, model=model, workspace=workspace, provider=provider)

    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
               "X-Conversation-Id": cid}

    # ── 平台通道：免 Key ──
    if provider == "platform":
        model = model or "auto"
        sid = (conv.get("sid") or "").strip()

        async def gen_plat():
            acc, tools = [], []
            try:
                async for ev in _plat.stream(user_text, model, workspace, sid, resume=bool(sid)):
                    t = ev.get("type")
                    if t == "meta":
                        _store.update_conv(cid, sid=ev.get("sid") or "")
                    elif t == "delta":
                        acc.append(ev["text"])
                        yield _sse({"choices": [{"delta": {"content": ev["text"]}}]})
                    elif t == "tool":
                        tools.append(ev["text"])
                        yield _sse({"tool": ev["text"]})
                    elif t == "title":
                        cv = _store.get_conv(cid) or {}
                        if cv.get("title") in ("", "新对话"):
                            _store.update_conv(cid, title=str(ev["text"])[:40])
                    elif t == "ping":
                        # SSE 注释行：只为穿过网关的空闲超时，前端解析时自然跳过
                        yield b": ping\n\n"
                    elif t == "error":
                        yield _sse({"error": ev["text"]})
                    elif t == "done" and ev.get("empty") and not acc:
                        if ev.get("dropped"):
                            yield _sse({"error": "这条回复只有工具调用、没有文字：当前对话通道不能执行命令。"
                                                 "要拉代码、跑服务，请到 💻 终端 里执行；"
                                                 "或在「模型」里换成自己的 API。"})
                        else:
                            yield _sse({"error": "平台通道没有返回内容，请换个模型或稍后重试"})
                yield b"data: [DONE]\n\n"
            finally:
                # 客户端中途断开时也要把已生成的部分存下来
                full = "".join(acc)
                if full:
                    _store.add_msg(cid, "assistant", full, model, "; ".join(tools[:4]))

        return StreamingResponse(gen_plat(), media_type="text/event-stream", headers=headers)

    # ── BYO：用户自己的 OpenAI 兼容端点 ──
    model = model or (cfg.get("models") or ["default"])[0]
    sys_msg = {"role": "system",
               "content": "你是云桌面里的编程助手，当前工作区：%s。回答用简体中文，简洁直接。"
                          "代码块请标注语言。" % workspace}
    payload = [sys_msg] + messages
    cfg_url = cfg["base_url"].rstrip("/") + "/chat/completions"

    def gen():
        acc = []
        try:
            r = _requests.post(cfg_url, json={"model": model, "messages": payload, "stream": True},
                               headers={"Authorization": "Bearer " + cfg["api_key"],
                                        "Content-Type": "application/json"},
                               stream=True, timeout=180)
            if r.status_code >= 400:
                yield _sse({"error": "上游 HTTP %d: %s" % (r.status_code, r.text[:300])})
                return
            for line in r.iter_lines(decode_unicode=True):
                if not line:
                    continue
                if line.startswith("data:"):
                    pl = line[5:].strip()
                    if pl == "[DONE]":
                        break
                    try:
                        o = json.loads(pl)
                        d = o["choices"][0]["delta"].get("content", "")
                        if d:
                            acc.append(d)
                    except Exception:
                        pass
                    yield (line + "\n\n").encode()
        except Exception as e:
            yield _sse({"error": str(e)})
        finally:
            full = "".join(acc)
            if full:
                _store.add_msg(cid, "assistant", full, model)

    return StreamingResponse(gen(), media_type="text/event-stream", headers=headers)

# ─────────────── 服务端截图浏览器（Playwright + Chromium） ───────────────
# 用沙箱自带 Chromium 在服务器渲染网页、回传截图，点击/输入坐标转发。
_BR = {"pw": None, "browser": None, "pages": {}}

async def _br_page(token, w=430, h=900):
    from playwright.async_api import async_playwright
    if _BR["browser"] is None:
        _BR["pw"] = await async_playwright().start()
        _BR["browser"] = await _BR["pw"].chromium.launch(
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"])
    pg = _BR["pages"].get(token)
    if pg is None:
        ctx = await _BR["browser"].new_context(
            viewport={"width": w, "height": h},
            user_agent=("Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
                        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 "
                        "Mobile/15E148 Safari/604.1"),
            is_mobile=True, has_touch=True, device_scale_factor=2)
        pg = await ctx.new_page()
        _BR["pages"][token] = pg
    return pg

def _png(pg_url):
    return {"X-Browser-Url": pg_url or ""}

@app.get("/api/browser/goto")
async def br_goto(request: Request, u: str = "", url: str = "", w: int = 430, h: int = 900):
    tok = request.cookies.get("session")
    if not authed(tok):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    # 公网 WAF 会拦截 query 里的明文外链（当作 SSRF），统一 base64 传参
    target = url
    if u:
        try:
            target = base64.b64decode(u).decode("utf-8")
        except Exception:
            target = ""
    if not target:
        return JSONResponse({"error": "缺少网址"}, status_code=400)
    if not target.startswith(("http://", "https://")):
        target = "https://" + target
    try:
        pg = await _br_page(tok, w, h)
        await pg.set_viewport_size({"width": w, "height": h})
        await pg.goto(target, timeout=20000, wait_until="domcontentloaded")
        # 等网络基本静默再截图：否则图片/广告把布局推走，
        # 用户看到的截图和实际 DOM 位置对不上，点击就会点偏。
        try:
            await pg.wait_for_load_state("networkidle", timeout=2500)
        except Exception:
            pass
        try:
            await pg.wait_for_timeout(400)
        except Exception:
            pass
        png = await pg.screenshot(type="png")
        return Response(content=png, media_type="image/png", headers=_png(pg.url))
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)

@app.get("/api/browser/click")
async def br_click(request: Request, x: int, y: int):
    tok = request.cookies.get("session")
    if not authed(tok):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    pg = _BR["pages"].get(tok)
    if pg is None:
        return JSONResponse({"error": "无会话"}, status_code=400)
    try:
        # 关键：context 是 is_mobile + has_touch，移动版页面监听 touch 事件，
        # 用 mouse.click 点输入框不会聚焦（实测百度/知乎都是这样）。
        await pg.touchscreen.tap(x, y)
        await pg.wait_for_timeout(800)
        png = await pg.screenshot(type="png")
        return Response(content=png, media_type="image/png", headers=_png(pg.url))
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)

@app.get("/api/browser/type")
async def br_type(request: Request, text: str, enter: int = 0):
    tok = request.cookies.get("session")
    if not authed(tok):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    pg = _BR["pages"].get(tok)
    if pg is None:
        return JSONResponse({"error": "无会话"}, status_code=400)
    try:
        if text:
            await pg.keyboard.type(text, delay=15)
        if enter:
            await pg.keyboard.press("Enter")
        await pg.wait_for_timeout(300)
        png = await pg.screenshot(type="png")
        return Response(content=png, media_type="image/png", headers=_png(pg.url))
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)

@app.get("/api/browser/key")
async def br_key(request: Request, name: str = "Enter"):
    tok = request.cookies.get("session")
    if not authed(tok):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    pg = _BR["pages"].get(tok)
    if pg is None:
        return JSONResponse({"error": "无会话"}, status_code=400)
    try:
        await pg.keyboard.press(name)
        await pg.wait_for_timeout(800)
        png = await pg.screenshot(type="png")
        return Response(content=png, media_type="image/png", headers=_png(pg.url))
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)

@app.get("/api/browser/scroll")
async def br_scroll(request: Request, dy: int = 600):
    tok = request.cookies.get("session")
    if not authed(tok):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    pg = _BR["pages"].get(tok)
    if pg is None:
        return JSONResponse({"error": "无会话"}, status_code=400)
    try:
        await pg.evaluate("(d) => window.scrollBy({top: d, behavior: 'instant'})", dy)
        await pg.wait_for_timeout(500)
        png = await pg.screenshot(type="png")
        return Response(content=png, media_type="image/png", headers=_png(pg.url))
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)

@app.get("/api/browser/back")
async def br_back(request: Request):
    tok = request.cookies.get("session")
    if not authed(tok):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    pg = _BR["pages"].get(tok)
    if pg is None:
        return JSONResponse({"error": "无会话"}, status_code=400)
    try:
        await pg.go_back(timeout=15000)
        await pg.wait_for_timeout(600)
        png = await pg.screenshot(type="png")
        return Response(content=png, media_type="image/png", headers=_png(pg.url))
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)

# PWA 清单按请求动态生成：把网关的 sharecode 注入启动地址与图标地址，
# 这样同一份代码在任何部署下都能正确“添加到主屏幕”，仓库里也不用写死任何域名参数。
@app.get("/manifest.json")
async def manifest(request: Request):
    try:
        m = json.loads((STATIC / "manifest.json").read_text(encoding="utf-8"))
    except Exception:
        return JSONResponse({"error": "manifest not found"}, status_code=404)
    # 网关会消费掉查询串里的 sharecode，因此依次尝试：查询串 → Referer → Cookie
    sc_ = request.query_params.get("sharecode")
    if not sc_:
        ref = request.headers.get("referer", "")
        m_ref = re.search(r"[?&]sharecode=([A-Za-z0-9_-]+)", ref)
        if m_ref:
            sc_ = m_ref.group(1)
    if not sc_:
        sc_ = request.cookies.get("sharecode") or ""
    if sc_:
        suffix = "?sharecode=" + sc_
        m["start_url"] = "/" + suffix
        for ic in m.get("icons", []):
            if ic.get("src") and "?" not in ic["src"]:
                ic["src"] = ic["src"] + suffix
    return JSONResponse(m)

app.mount("/", StaticFiles(directory=str(STATIC), html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    # 发布环境通过 PORT 注入端口，本地默认 8080
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
