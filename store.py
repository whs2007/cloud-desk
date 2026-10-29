"""会话持久化层（SQLite）

为什么用 SQLite 而不是 jsonl：
- 多会话 → 需要按 conv_id 查询/分页，jsonl 只能全量扫描
- 并发写入 → SQLite WAL 模式下读写不互斥，jsonl 追加遇到并发会撕裂
- 单文件、零依赖、随项目走，备份就是拷一个 .db 文件

表结构刻意保持极简，方便以后换 Postgres：两张表，无外键约束。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path

BASE = Path(__file__).parent
DB_PATH = BASE / "data" / "desk.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
  id         TEXT PRIMARY KEY,
  title      TEXT NOT NULL DEFAULT '新对话',
  workspace  TEXT NOT NULL DEFAULT '/workspace',
  model      TEXT NOT NULL DEFAULT '',
  provider   TEXT NOT NULL DEFAULT 'platform',
  sid        TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  conv_id TEXT NOT NULL,
  role    TEXT NOT NULL,
  content TEXT NOT NULL DEFAULT '',
  model   TEXT NOT NULL DEFAULT '',
  tool    TEXT NOT NULL DEFAULT '',
  ts      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_msg_conv ON messages(conv_id, id);
CREATE INDEX IF NOT EXISTS idx_conv_upd ON conversations(updated_at DESC);
"""

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None


def _c() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=10)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA synchronous=NORMAL")
        _conn.executescript(SCHEMA)
        _conn.commit()
    return _conn


def _now() -> int:
    return int(time.time())


def new_conv(workspace: str = "/workspace", model: str = "",
             provider: str = "platform", title: str = "新对话") -> dict:
    cid = uuid.uuid4().hex[:12]
    ts = _now()
    with _lock:
        _c().execute(
            "INSERT INTO conversations(id,title,workspace,model,provider,sid,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (cid, title, workspace, model, provider, "", ts, ts))
        _c().commit()
    return get_conv(cid)


def get_conv(cid: str) -> dict | None:
    with _lock:
        r = _c().execute("SELECT * FROM conversations WHERE id=?", (cid,)).fetchone()
    return dict(r) if r else None


def list_convs(limit: int = 100) -> list[dict]:
    with _lock:
        rows = _c().execute(
            "SELECT c.*, (SELECT COUNT(*) FROM messages m WHERE m.conv_id=c.id) AS n"
            " FROM conversations c ORDER BY c.updated_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def update_conv(cid: str, **kw) -> None:
    allowed = {"title", "workspace", "model", "provider", "sid"}
    sets, vals = [], []
    for k, v in kw.items():
        if k in allowed:
            sets.append("%s=?" % k)
            vals.append(v)
    if not sets:
        return
    sets.append("updated_at=?")
    vals.append(_now())
    vals.append(cid)
    with _lock:
        _c().execute("UPDATE conversations SET %s WHERE id=?" % ",".join(sets), vals)
        _c().commit()


def touch(cid: str) -> None:
    with _lock:
        _c().execute("UPDATE conversations SET updated_at=? WHERE id=?", (_now(), cid))
        _c().commit()


def del_conv(cid: str) -> None:
    with _lock:
        _c().execute("DELETE FROM messages WHERE conv_id=?", (cid,))
        _c().execute("DELETE FROM conversations WHERE id=?", (cid,))
        _c().commit()


def add_msg(cid: str, role: str, content: str, model: str = "", tool: str = "") -> int:
    if not content and not tool:
        return 0
    ts = _now()
    with _lock:
        cur = _c().execute(
            "INSERT INTO messages(conv_id,role,content,model,tool,ts) VALUES(?,?,?,?,?,?)",
            (cid, role, content, model, tool, ts))
        _c().execute("UPDATE conversations SET updated_at=? WHERE id=?", (ts, cid))
        _c().commit()
        return cur.lastrowid or 0


def msgs(cid: str, limit: int = 500) -> list[dict]:
    with _lock:
        rows = _c().execute(
            "SELECT * FROM messages WHERE conv_id=? ORDER BY id ASC LIMIT ?",
            (cid, limit)).fetchall()
    return [dict(r) for r in rows]


def auto_title(cid: str, text: str) -> None:
    """首条用户消息前 18 字作为标题；只在仍是默认标题时生效。"""
    t = " ".join((text or "").split())
    if not t:
        return
    conv = get_conv(cid)
    if not conv or conv.get("title") not in ("", "新对话"):
        return
    update_conv(cid, title=t[:14] + ("…" if len(t) > 14 else ""))


def import_jsonl(path: Path) -> int:
    """把老版本的 chat.jsonl 一次性迁进默认会话。返回导入条数。"""
    if not path or not path.exists():
        return 0
    with _lock:
        n = _c().execute("SELECT COUNT(*) n FROM messages").fetchone()["n"]
    if n:
        return 0                      # 已有数据，不重复导入
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return 0
    recs = []
    for ln in lines:
        try:
            r = json.loads(ln)
        except Exception:
            continue
        if r.get("content"):
            recs.append(r)
    if not recs:
        return 0
    conv = new_conv(title="历史消息（已迁移）")
    for r in recs:
        add_msg(conv["id"], r.get("role", "user"), r.get("content", ""), r.get("model", ""))
    return len(recs)


def stats() -> dict:
    with _lock:
        c = _c().execute("SELECT COUNT(*) n FROM conversations").fetchone()["n"]
        m = _c().execute("SELECT COUNT(*) n FROM messages").fetchone()["n"]
    return {"conversations": c, "messages": m, "db": str(DB_PATH)}
