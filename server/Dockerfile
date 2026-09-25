# syntax=docker/dockerfile:1

# Tindarr server image, with the web console inside it.
# - Build context: the REPOSITORY ROOT, because the console needs web/,
#   shared/i18n/ and shared/design/ (the contract is not read here: the generated
#   client web/src/api/schema.d.ts is committed):
#       docker build -f server/Dockerfile -t tindarr-server .
#   The root .dockerignore says what the build may read.
# - Runs as UID/GID 10001, works with a read-only root filesystem: state lives in the
#   /data volume, scratch space in a tmpfs on /tmp.
# - Listens on port 8787 (TINDARR_PORT). The runtime stage has no Node.

ARG PYTHON_IMAGE=python:3.13-slim-trixie@sha256:8d9d0b8bcf6506481eae4907c18f5e3e7902e629f5f6d684f9e7c32e85e3ddf0
ARG UV_IMAGE=astral/uv:0.12.17@sha256:10787c682e4184e4f290de1171fd4703dc63de99221f10fe1c99002ce7fa9acc
ARG NODE_IMAGE=node:24.21.0-trixie-slim@sha256:8ec5d7557396cfe32d21c3f9c13072355ceab22b584578ca4bb28af31120cffe

FROM ${UV_IMAGE} AS uv

# ---------------------------------------------------------------- web console
# Static files only, so it always builds for the build platform.
FROM --platform=$BUILDPLATFORM ${NODE_IMAGE} AS web
WORKDIR /src/web

# Dependencies first, from the lock file only, for a cacheable layer.
COPY web/package.json web/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm \
    npm ci --ignore-scripts --no-audit --no-fund

# The console reads the shared catalogs through its @i18n alias, and the design
# tokens both clients share through @design.
COPY shared/i18n /src/shared/i18n
COPY shared/design /src/shared/design
COPY web/ ./
# Builds, then refuses a bundle the console's CSP would break. Linting, types
# and tests run in web.yml, not here.
RUN npm run build

# ---------------------------------------------------------------- server build
FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/local/bin/python3 \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src

RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=server/uv.lock,target=uv.lock \
    --mount=type=bind,source=server/pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-dev --no-install-project

COPY server/pyproject.toml server/uv.lock server/README.md ./
COPY server/src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

# ---------------------------------------------------------------- runtime
FROM ${PYTHON_IMAGE} AS runtime

LABEL org.opencontainers.image.title="Tindarr server" \
      org.opencontainers.image.description="AI-picked movie and series cards to swipe, requests filed to Seerr." \
      org.opencontainers.image.source="https://github.com/akira783/tindarr" \
      org.opencontainers.image.licenses="MIT"

RUN groupadd --system --gid 10001 tindarr \
 && useradd --system --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent \
        --shell /usr/sbin/nologin tindarr \
 && install -d -o 10001 -g 10001 -m 0700 /data

COPY --from=build /app/.venv /app/.venv
# Read-only for the server user: the console is never written to at run time.
COPY --from=web --chown=root:root --chmod=755 /src/web/dist /app/web

ENV PATH=/app/.venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TINDARR_DATA_DIR=/data \
    TINDARR_WEB_DIR=/app/web \
    TINDARR_HOST=0.0.0.0 \
    TINDARR_PORT=8787

USER 10001:10001
WORKDIR /data
VOLUME ["/data"]
EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["tindarr", "healthcheck"]

ENTRYPOINT ["tindarr"]
CMD ["serve"]
