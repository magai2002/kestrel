# kestrel engine / CLI image. One image, several roles (engine-sim,
# engine-sandbox, migrate, api from Phase 6): the command differs.
#
# uv is copied from its official image at the exact version used to
# write uv.lock, and the lockfile is honoured (--frozen): the image
# runs exactly the dependency set CI tested.
#
# Size notes (first build was 2.55 GB for a 605 MB venv): no uv cache in
# any layer (UV_NO_CACHE), and files are created by the runtime user
# rather than chown -R'd afterwards, which would copy the whole venv into
# a second layer.
# --- dashboard (web/): built here so no Node toolchain is needed on the host
FROM node:22.11.0-alpine AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

FROM python:3.12-slim AS app

COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /usr/local/bin/uv

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv

RUN useradd --uid 10001 --create-home kestrel \
    && mkdir /app && chown kestrel:kestrel /app
USER kestrel
WORKDIR /app

# Dependencies first (cached unless the lockfile changes)...
COPY --chown=kestrel:kestrel pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

# ...then the code.
COPY --chown=kestrel:kestrel . .
RUN uv sync --frozen --no-dev

COPY --from=web --chown=kestrel:kestrel /web/dist /app/web/dist

ENV PATH="/app/.venv/bin:$PATH"
ENTRYPOINT ["kestrel"]
CMD ["--help"]
