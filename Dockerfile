# Stage 1: build the frontend. Stage 2: python + uv serving API, WebSocket and the built assets.
FROM node:22-alpine AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
# The Clerk publishable key is baked into the bundle at build time (Railway: set it as a build variable).
ARG VITE_CLERK_PUBLISHABLE_KEY
ENV VITE_CLERK_PUBLISHABLE_KEY=$VITE_CLERK_PUBLISHABLE_KEY
RUN npm run build

FROM python:3.13-slim
RUN pip install --no-cache-dir uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1
WORKDIR /app/backend
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev
COPY backend/ ./
COPY --from=web /web/dist /app/frontend/dist
# The /data volume is attached in the Railway service settings (Railway rejects VOLUME in Dockerfiles).
ENV DATABASE_PATH=/data/market.db PORT=8000
EXPOSE 8000
# Exactly one worker: the in-memory rings, the WebSocket fan-out and the SQLite writer are per-process state.
CMD ["sh", "-c", "uv run --no-sync uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --workers 1 --proxy-headers --forwarded-allow-ips='*'"]
