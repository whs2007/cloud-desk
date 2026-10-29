#!/bin/bash
# 云桌面工作台 · 一键启动
# 用法: bash /workspace/webos/start.sh  （沙箱休眠后执行此脚本即可复活服务）
set -e
cd "$(dirname "$0")"

PORT="${PORT:-8080}"

# 端口被占则自动顺延
while ss -tln 2>/dev/null | grep -q ":$PORT "; do
  echo "端口 $PORT 已占用，尝试 $((PORT+1))"
  PORT=$((PORT+1))
done

echo "启动云桌面工作台 → http://localhost:$PORT"
echo "真实配额: $(cat /sys/fs/cgroup/cpu.max 2>/dev/null | awk '{printf "%.1f 核", $1/$2}') / 内存 $(( $(cat /sys/fs/cgroup/memory.max 2>/dev/null) / 1073741824 )) GB"
echo "按 Ctrl+C 停止"
exec python3 -m uvicorn server:app --host 0.0.0.0 --port "$PORT" --log-level info
