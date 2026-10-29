# Multi-stage build: frontend assets, Codex CLI, and the Python runtime.
# Optional: --build-arg USE_CN_MIRROR=1 switches npm / PyPI / Debian to China-based mirrors.
# Optional Node.js provider support is disabled by default.
ARG USE_CN_MIRROR=0
ARG INCLUDE_STOCKSDK=0
ARG NPM_REGISTRY=https://registry.npmmirror.com
ARG PYPI_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple
# 备用 PyPI 源:主源同步延迟/故障时自动兜底(阿里云与清华互为补充)
ARG PYPI_FALLBACK=https://mirrors.aliyun.com/pypi/simple
ARG BACKEND_EXTRAS=
ARG CODEX_CLI_VERSION=0.144.3

# === Stage 1: 前端构建 ===
FROM node:20-alpine AS frontend-builder
ARG USE_CN_MIRROR=0
ARG NPM_REGISTRY=https://registry.npmmirror.com
WORKDIR /build
# 关键:corepack 不读 npm 的 registry 配置,且跨 RUN 不保留环境变量,
# 因此国内网络下最稳的做法是直接用 npm 安装 pnpm(npm 会读取 .npmrc 镜像源),
# 彻底绕开 corepack 再次联网下载 pnpm 的问题。
RUN if [ "$USE_CN_MIRROR" = "1" ]; then npm config set registry "$NPM_REGISTRY"; fi && \
    npm install -g pnpm@9
# 让 pnpm 走镜像源安装依赖
RUN if [ "$USE_CN_MIRROR" = "1" ]; then pnpm config set registry "$NPM_REGISTRY"; fi
COPY frontend/package.json frontend/pnpm-lock.yaml* ./
RUN pnpm install --frozen-lockfile || pnpm install
COPY frontend/ ./
RUN pnpm build

# === Stage 2: Codex CLI ===
# 固定版本保证镜像可复现；只复制安装产物到运行镜像，不保留 npm。
FROM node:20-bookworm-slim AS codex-builder
ARG USE_CN_MIRROR=0
ARG NPM_REGISTRY=https://registry.npmmirror.com
# 版本由顶层 ARG CODEX_CLI_VERSION 提供, 这里仅声明以继承, 不再重复默认值。
ARG CODEX_CLI_VERSION
RUN if [ "$USE_CN_MIRROR" = "1" ]; then npm config set registry "$NPM_REGISTRY"; fi \
    && npm install --global --prefix /opt/codex "@openai/codex@${CODEX_CLI_VERSION}" \
    && CODEX_NATIVE="$(find /opt/codex -type f -path '*/vendor/*/bin/codex' -print -quit)" \
    && test -n "$CODEX_NATIVE" \
    && cp "$CODEX_NATIVE" /opt/codex-native \
    && chmod +x /opt/codex-native \
    && /opt/codex-native --version

# === Stage 2: Python 运行时 ===
FROM python:3.11-slim AS runtime
ARG USE_CN_MIRROR=0
ARG PYPI_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PYPI_FALLBACK=https://mirrors.aliyun.com/pypi/simple
ARG BACKEND_EXTRAS=
ARG INCLUDE_STOCKSDK=0
WORKDIR /app

# Node.js 运行时: 仅在启用 stock-sdk 插件时安装(供 node bridge.mjs 使用)。
# Codex CLI 从官方 npm 包提取原生二进制，不依赖运行时 Node.js。
# bookworm 自带 nodejs 18.19, 满足插件 engines>=18; --no-install-recommends 精简,
# 自带 libnode/libc-ares 等全部动态依赖, 无需手动补库。
# 国内构建换 apt 阿里云镜像 (bookworm deb822 格式)。官方 python slim 镜像的
# sources 指向 deb.debian.org, 部分国内网络下 apt 极慢(实测阿里云 ECS 拉官方源
# 单阶段可达 8 分钟以上); USE_CN_MIRROR 与 npm/pypi 的换源开关注一脉相承。
# tesseract-ocr: 自选截图导入（始终安装）; nodejs: 仅 INCLUDE_STOCKSDK=1 时安装
RUN if [ "$USE_CN_MIRROR" = "1" ]; then \
        sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources /etc/apt/sources.list 2>/dev/null || true; \
    fi \
    && apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng \
    && if [ "$INCLUDE_STOCKSDK" = "1" ]; then \
         apt-get install -y --no-install-recommends nodejs \
         && node --version; \
       fi \
    && rm -rf /var/lib/apt/lists/* \
    && tesseract --version

# 安装 uv(快) —— 国内镜像下三重兜底:主源 → 备用源 → 官方源,
# 任一成功即可,避免单一镜像同步延迟/故障导致构建失败。
# uv 发版极频繁,国内镜像同步存在时间窗口,不锁版本且无 fallback 时
# 容易遇到 "from versions: none"(索引解析不到最新版)。
RUN if [ "$USE_CN_MIRROR" = "1" ]; then \
      pip install --no-cache-dir uv -i "$PYPI_INDEX" || \
      pip install --no-cache-dir uv -i "$PYPI_FALLBACK" || \
      pip install --no-cache-dir uv; \
    else \
      pip install --no-cache-dir uv; \
    fi

# Backend deps
COPY README.md /README.md
COPY LICENSE LICENSE.upstream NOTICE.md THIRD_PARTY_NOTICES.md /licenses/
# LICENSES/ (第三方许可全文) 只在完整版存在; 通配让核心版没有该目录时也能构建
COPY LICENSE[S] /licenses/LICENSES
COPY backend/pyproject.toml backend/uv.lock* ./
# uv 原生支持同时挂多个 index(主源 + 备用源),会自动在两源中查找,
# 比逐个重试更稳健 —— 任一源缺包时另一源补位。
RUN if [ "$USE_CN_MIRROR" = "1" ]; then \
      export UV_DEFAULT_INDEX="$PYPI_INDEX" UV_EXTRA_INDEX_URL="$PYPI_FALLBACK"; \
    fi; \
    set -- --no-dev; \
    for extra in $BACKEND_EXTRAS; do \
      set -- "$@" --extra "$extra"; \
    done; \
    uv sync --frozen "$@" || uv sync "$@"

# Backend code
# 注意:Docker 里 WORKDIR=/app, 而 config.py 的 _PROJECT_ROOT 是按开发布局
# (<root>/backend/app/) 推导的, 容器内会错算到 /。这里用环境变量显式指定
# 三个关键路径, 确保 static / data 都指向容器内正确位置。
COPY backend/app ./app
# Strategy and opportunity defaults are data, not runtime user state.
COPY backend/config ./config
ENV STATIC_DIR=/app/static \
    DATA_DIR=/app/data \
    TICKFLOW_ENV_FILE=/app/.env

# Frontend 静态产物
COPY --from=frontend-builder /build/dist ./static

# Codex CLI 使用官方 npm 包携带的当前平台原生二进制，无需运行时 Node.js。
COPY --from=codex-builder /opt/codex-native /usr/local/bin/codex
RUN codex --version

ENV PYTHONPATH=/app
# 运行时只使用构建期装好的依赖: --no-sync 让 `uv run` 不在启动时重新解析或联网安装。
ENV UV_NO_SYNC=1
# Trading logic uses app.market_clock.us_clock (America/New_York). TZ affects log timestamps
# and a few local-date defaults (e.g. backtest end date); override with -e TZ=... if needed.
ENV TZ=Asia/Shanghai
ARG ZENTRADE_COMMIT=dev
LABEL org.opencontainers.image.revision=${ZENTRADE_COMMIT} \
      org.opencontainers.image.title="ZenTrade" \
      org.opencontainers.image.description="Self-hosted US swing-trading research, monitoring and paper-trading workspace" \
      org.opencontainers.image.source="https://github.com/wuyangfan234/zentrade" \
      org.opencontainers.image.licenses="MIT AND LicenseRef-ZenTrade-Third-Party"
EXPOSE 3018
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:3018/health', timeout=4).status == 200 else 1)"
CMD ["uv", "run", "--no-sync", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "3018"]
