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

# Create non-root user (UID 1000) for Hugging Face Spaces compatibility
RUN useradd -m -u 1000 user

# Install python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --prefer-binary -r requirements.txt

# Copy backend application source
COPY app/ ./app/
COPY core/ ./core/
COPY explore/ ./explore/
COPY formats/ ./formats/
COPY evaluation/ ./evaluation/
COPY analyst/ ./analyst/
COPY charts/ ./charts/
COPY charts_ai/ ./charts_ai/
COPY connectors/ ./connectors/
COPY demo.py .

# Copy built frontend from Stage 1
COPY --from=frontend-builder /build/dist ./frontend/dist

# Set ownership of /app to user 1000 for non-root execution
RUN mkdir -p /app/data && chown -R user:user /app

USER user

# Support both custom port (Hugging Face default 7860 or standard 8000)
ENV PORT=8000
EXPOSE 8000
EXPOSE 7860

# Healthcheck
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:${PORT}/api/health || exit 1

# Start FastAPI server with long keep-alive for multi-gigabyte uploads
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --timeout-keep-alive 3600"]
