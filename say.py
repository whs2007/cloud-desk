#!/usr/bin/env python3
"""把 WorkBuddy 会话里的回复追加到 chat.jsonl，云桌面的对话窗口会轮询显示。

用法：
    echo "内容" | python3 /workspace/webos/say.py
    python3 /workspace/webos/say.py <<'EOF'
    多行内容
    EOF
"""
import sys, json, time
from pathlib import Path

p = Path(__file__).parent / "data" / "chat.jsonl"
p.parent.mkdir(exist_ok=True)

text = sys.stdin.read()
if not text.strip():
    print("(空内容，未写入)")
    sys.exit(0)

rec = {
    "role": "assistant",
    "content": text.strip(),
    "ts": int(time.time()),
    "src": "workbuddy",
}
with p.open("a", encoding="utf-8") as f:
    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
print("ok: 已同步 %d 字" % len(text.strip()))
