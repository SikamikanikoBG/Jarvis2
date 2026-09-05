# Jarvis V2 core — multi-stage: build the SPA, then a slim Python runtime.
FROM node:20-alpine AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM python:3.12-slim AS runtime
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1 \
    JARVIS_HOME=/data JARVIS_HOST=0.0.0.0 JARVIS_PORT=9020 JARVIS_WEB_DIST=/app/web/dist TZ=Europe/Sofia
COPY pyproject.toml uv.lock ./
COPY packages ./packages
RUN uv sync --all-packages --no-dev --frozen
COPY --from=web /web/dist ./web/dist
VOLUME ["/data"]
EXPOSE 9020
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:9020/api/health',timeout=4).status==200 else 1)" || exit 1
CMD ["uv", "run", "--no-sync", "jarvis-core"]
