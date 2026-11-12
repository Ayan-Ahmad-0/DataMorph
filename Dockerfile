# ==========================================
# Stage 1: Build React Frontend
# ==========================================
FROM node:20-slim AS frontend-builder
WORKDIR /build

COPY frontend/package*.json ./
RUN npm ci

COPY frontend/ ./
RUN npx vite build

# ==========================================
# Stage 2: Python Backend Runtime
# ==========================================
FROM python:3.12-slim AS runner

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --prefer-binary -r requirements.txt

# Copy backend application source (runtime modules only)
COPY app/ ./app/
COPY core/ ./core/
COPY explore/ ./explore/
COPY formats/ ./formats/
COPY analyst/ ./analyst/
COPY charts/ ./charts/
COPY charts_ai/ ./charts_ai/
COPY connectors/ ./connectors/

# Copy built frontend from Stage 1
COPY --from=frontend-builder /build/dist ./frontend/dist

# Create non-root runtime user and dataset storage directory
RUN addgroup --system appuser && adduser --system --ingroup appuser appuser \
    && mkdir -p /app/data \
    && chown -R appuser:appuser /app

USER appuser

# Support both custom port (standard 8000 or custom)
ENV PORT=8000
EXPOSE 8000

# Healthcheck
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:${PORT}/api/health || exit 1

# Start FastAPI server with long keep-alive for multi-gigabyte uploads
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --timeout-keep-alive 3600"]
