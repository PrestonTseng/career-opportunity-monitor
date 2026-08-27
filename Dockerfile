FROM ghcr.io/astral-sh/uv@sha256:b1e699368d24c57cda93c338a57a8c5a119009ba809305cc8e86986d4a006754 AS uv

FROM python:3.13.5-slim-bookworm@sha256:4c2cf9917bd1cbacc5e9b07320025bdb7cdf2df7b0ceaccb55e9dd7e30987419

ARG SUPERCRONIC_VERSION=v0.2.49

COPY --from=uv /uv /uvx /bin/

RUN set -eux; \
    detected_arch="$(dpkg --print-architecture)"; \
    case "$detected_arch" in \
      amd64) supercronic_arch=amd64; supercronic_sha=a53ae236602c7338aba3fbaff40bda6300eae3b9fedb8261eb06cfe3724430c1 ;; \
      arm64) supercronic_arch=arm64; supercronic_sha=02aa0cb229ba09050cba6638059dadb9eedc2276632ea43d6a57a2f8c1629dd5 ;; \
      *) echo "unsupported architecture: $detected_arch" >&2; exit 1 ;; \
    esac; \
    python -c "import urllib.request; urllib.request.urlretrieve('https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-${supercronic_arch}', '/usr/local/bin/supercronic')"; \
    echo "${supercronic_sha}  /usr/local/bin/supercronic" | sha256sum -c -; \
    chmod 0755 /usr/local/bin/supercronic; \
    groupadd --gid 10001 career-monitor; \
    useradd --uid 10001 --gid 10001 --create-home --home-dir /home/career-monitor career-monitor; \
    install -d -o 10001 -g 10001 -m 0700 /data

WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY schemas ./schemas
COPY src ./src
RUN uv sync --frozen --no-dev && uv cache clean

USER 10001:10001

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD ["career-monitor", "validate"]

ENTRYPOINT ["career-monitor"]
CMD ["validate"]
