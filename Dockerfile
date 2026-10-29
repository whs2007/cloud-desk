FROM python:3.11-slim

# WITH_BROWSER=1 会额外安装 Chromium（内置截图浏览器需要，镜像会大 400MB 左右）
ARG WITH_BROWSER=0

ENV PYTHONUNBUFFERED=1 \
    PORT=8080 \
    WORKSPACE_ROOT=/workspace \
    DEBIAN_FRONTEND=noninteractive

# 终端里常用的基础工具（这是个能写代码的终端，不是纯服务）
# tmux 用于托管终端会话：关页面、重启服务、换实例都不会丢
RUN apt-get update && apt-get install -y --no-install-recommends \
        bash curl wget git vim nano less unzip zip procps iproute2 tmux \
        ca-certificates locales \
    && sed -i 's/^# *en_US.UTF-8/en_US.UTF-8/' /etc/locale.gen \
    && locale-gen \
    && rm -rf /var/lib/apt/lists/*

ENV LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && if [ "$WITH_BROWSER" = "1" ]; then python -m playwright install --with-deps chromium; fi

COPY . .
RUN mkdir -p /workspace

EXPOSE 8080
CMD ["python", "server.py"]
