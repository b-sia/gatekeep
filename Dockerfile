FROM node:20-slim AS frontend-build
WORKDIR /app/dashboard
COPY dashboard/package*.json ./
RUN npm ci
COPY dashboard ./
RUN npm run build

FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.12.22 /uv /usr/local/bin/uv
WORKDIR /app
# Install exactly the versions pinned in uv.lock, so a rebuild never silently
# picks up new upstream releases. --locked also fails the build if uv.lock is
# out of date with pyproject.toml, instead of installing a stale dependency set.
# Dependencies go in their own layer, before the source, so code-only changes
# reuse the cached install.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/app/.venv/bin:$PATH"
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-install-project
COPY gatekeep ./gatekeep
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev
# Bake the semantic-cache embedding model's weights into the image at build
# time, so a fresh container never needs to hit the HF Hub at runtime - that
# network fetch (plus any read timeouts/retries) previously stalled the
# first request to reach embed_text() by up to ~100s.
RUN python -c "from gatekeep.caching.embeddings import warm; warm()"
COPY migrations ./migrations
COPY alembic.ini ./
COPY --from=frontend-build /app/dashboard/dist ./dashboard/dist

EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && uvicorn gatekeep.app:app --host 0.0.0.0 --port 8000"]
