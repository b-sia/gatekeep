FROM node:20-slim AS frontend-build
WORKDIR /app/dashboard
COPY dashboard/package*.json ./
RUN npm ci
COPY dashboard ./
RUN npm run build

FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY gatekeep ./gatekeep
RUN pip install --no-cache-dir -e .
# Bake the semantic-cache embedding model's weights into the image at build
# time, so a fresh container never needs to hit the HF Hub at runtime - that
# network fetch (plus any read timeouts/retries) previously stalled the
# first request to reach embed_text() by up to ~100s.
RUN python -c "from gatekeep.embeddings import warm; warm()"
COPY migrations ./migrations
COPY alembic.ini ./
COPY --from=frontend-build /app/dashboard/dist ./dashboard/dist

EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && uvicorn gatekeep.app:app --host 0.0.0.0 --port 8000"]
