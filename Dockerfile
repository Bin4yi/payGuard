# PayGuard Stage 0 demo image: the web app and the API in one container,
# same origin. Runs the in-memory demo backend (services/api/main.py), so it
# MUST be deployed as exactly one instance — see docs/DEPLOYMENT.md.

# ---- 1. build the web app ----
FROM node:22-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
# Empty base URL = call the API on the same origin that served the page.
ENV VITE_API_BASE_URL=""
RUN npm run build

# ---- 2. runtime ----
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PAYGUARD_WEB_DIST=/app/web/dist \
    PAYGUARD_SCREENER=laya-hf \
    PORT=8080
WORKDIR /app
COPY pyproject.toml ./
COPY services/ ./services/
RUN pip install --no-cache-dir ".[laya-hf]"
COPY --from=web /web/dist ./web/dist
RUN useradd --uid 10001 --no-create-home payguard
USER payguard
EXPOSE 8080
# One worker on purpose: all state is in this process's memory.
CMD ["sh", "-c", "exec uvicorn services.api.main:app --host 0.0.0.0 --port ${PORT} --workers 1 --proxy-headers --forwarded-allow-ips='*'"]
