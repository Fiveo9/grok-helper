FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai \
    SERVER_HOST=0.0.0.0 \
    SERVER_PORT=8001 \
    DATA_DIR=/app/data \
    LOG_DIR=/app/logs \
    GROK_REGISTER_SOURCE_DIR=/app \
    GROK_REGISTER_PYTHON=/usr/local/bin/python

RUN apt-get update && apt-get install -y --no-install-recommends \
    xvfb \
    wget \
    gnupg \
    ca-certificates \
    fonts-noto-cjk \
    libasound2 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libc6 \
    libcairo2 \
    libcups2 \
    libdbus-1-3 \
    libdrm2 \
    libgbm1 \
    libglib2.0-0 \
    libgtk-3-0 \
    libnspr4 \
    libnss3 \
    libpango-1.0-0 \
    libx11-6 \
    libx11-xcb1 \
    libxcb1 \
    libxcomposite1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxrandr2 \
    xdg-utils \
    && rm -rf /var/lib/apt/lists/*

RUN wget -qO- https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor >/usr/share/keyrings/google-linux.gpg && \
    echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-linux.gpg] http://dl.google.com/linux/chrome/deb/ stable main" >/etc/apt/sources.list.d/google-chrome.list && \
    apt-get update && apt-get install -y --no-install-recommends google-chrome-stable && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY grok_helper ./grok_helper
COPY main.py config.example.json DrissionPage_example.py email_register.py sso_to_cpa.py ./
COPY turnstilePatch ./turnstilePatch
COPY app/statics ./app/statics

# 注册任务和服务日志都写入可挂载目录，避免容器重建丢状态。
RUN mkdir -p /app/data/register /app/logs

EXPOSE 8001

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["sh", "-c", "wget -qO /dev/null http://127.0.0.1:${SERVER_PORT}/health || exit 1"]

CMD ["sh", "-c", "granian --interface asgi --host ${SERVER_HOST} --port ${SERVER_PORT} --workers 1 main:app"]
