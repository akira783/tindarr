# syntax=docker/dockerfile:1

# Tindeerr server image.
# - Build context: this directory (server/).
# - Runs as UID/GID 10001, works with a read-only root filesystem: state lives in the
#   /data volume, scratch space in a tmpfs on /tmp.
# - Listens on port 8787 (TINDEERR_PORT).

ARG PYTHON_IMAGE=python:3.13-slim-trixie@sha256:8d9d0b8bcf6506481eae4907c18f5e3e7902e629f5f6d684f9e7c32e85e3ddf0
ARG UV_IMAGE=astral/uv:0.12.17@sha256:10787c682e4184e4f290de1171fd4703dc63de99221f10fe1c99002ce7fa9acc

FROM ${UV_IMAGE} AS uv

# ---------------------------------------------------------------- build
FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/local/bin/python3 \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src

# Dependencies first, from the lock file only, for a cacheable layer.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-dev --no-install-project

COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

# ---------------------------------------------------------------- runtime
FROM ${PYTHON_IMAGE} AS runtime

LABEL org.opencontainers.image.title="Tindeerr server" \
      org.opencontainers.image.description="AI-picked movie and series cards to swipe, requests filed to Seerr." \
      org.opencontainers.image.source="https://github.com/akira783/tindeerr" \
      org.opencontainers.image.licenses="MIT"

RUN groupadd --system --gid 10001 tindeerr \
 && useradd --system --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent \
        --shell /usr/sbin/nologin tindeerr \
 && install -d -o 10001 -g 10001 -m 0700 /data

COPY --from=build /app/.venv /app/.venv

ENV PATH=/app/.venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TINDEERR_DATA_DIR=/data \
    TINDEERR_HOST=0.0.0.0 \
    TINDEERR_PORT=8787

USER 10001:10001
WORKDIR /data
VOLUME ["/data"]
EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["tindeerr", "healthcheck"]

ENTRYPOINT ["tindeerr"]
CMD ["serve"]
