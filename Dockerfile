# ─────────────────────────────────────────────────────────────────────────────
# Dockerfile — Agente Conversacional
# Base: Python 3.11 slim para imagen liviana
# Destino: Azure Container Apps
# ─────────────────────────────────────────────────────────────────────────────

# ── Etapa 1: dependencias ─────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /app

# Instalar dependencias del sistema necesarias para compilar wheels
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip && \
    pip install --no-cache-dir --prefix=/install -r requirements.txt


# ── Etapa 2: imagen final ─────────────────────────────────────────────────────
FROM python:3.11-slim

WORKDIR /app

# Copiar dependencias instaladas desde builder
COPY --from=builder /install /usr/local

# Copiar código fuente
COPY src/       ./src/
COPY config/    ./config/

# Usuario no-root para seguridad en Container Apps
RUN adduser --disabled-password --gecos "" appuser && \
    chown -R appuser:appuser /app
USER appuser

# Variables de entorno por defecto (se sobreescriben en Container Apps)
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LOG_LEVEL=INFO \
    CONFIG_PATH=config/config_agent.yaml \
    PORT=8000

# Exponer puerto
EXPOSE 8000

# Health check para Container Apps liveness probe
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:${PORT}/health || exit 1

# Arrancar con uvicorn
CMD ["sh", "-c", "uvicorn src.api.main:app --host 0.0.0.0 --port ${PORT} --workers 1 --log-config /dev/null"]
