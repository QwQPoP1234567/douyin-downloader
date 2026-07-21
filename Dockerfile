FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        ca-certificates \
        chromium \
        chromium-sandbox \
        fonts-liberation \
        fonts-noto-cjk \
        fonts-noto-color-emoji \
        gosu \
        novnc \
        websockify \
        x11vnc \
        xvfb \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py README.md THIRD_PARTY_NOTICES.md ./
COPY app ./app
COPY alembic.ini ./
COPY migrations ./migrations
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

RUN groupadd --system --gid 10001 douyin \
    && useradd --system --uid 10001 --gid douyin --home-dir /app --shell /usr/sbin/nologin douyin \
    && mkdir -p /app/data /app/downloads /app/browser_data \
    && chown -R douyin:douyin /app \
    && chmod 1777 /tmp \
    && chmod 755 /usr/local/bin/docker-entrypoint.sh

VOLUME ["/app/data", "/app/downloads", "/app/browser_data"]

EXPOSE 8765 6080
STOPSIGNAL SIGTERM

HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=3 \
    CMD python -c "import json,urllib.request; r=urllib.request.urlopen('http://127.0.0.1:8765/api/health',timeout=3); assert json.load(r).get('ok')" || exit 1

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python", "main.py", "--host", "0.0.0.0", "--port", "8765"]
