# Companion AI OS — Production Container Image (Zero External Python Dependencies)
FROM python:3.12-slim

LABEL org.opencontainers.image.title="Companion AI OS" \
      org.opencontainers.image.description="Self-hosted, local-first AI Companion & Engineering OS with isolated user data" \
      org.opencontainers.image.licenses="MIT"

# Create non-root runtime user and isolated /data volume mountpoint
RUN groupadd --gid 1000 companion \
    && useradd --uid 1000 --gid 1000 --create-home --shell /bin/bash companion \
    && mkdir -p /app /data \
    && chown -R companion:companion /app /data

WORKDIR /app

# Copy application code & pre-built static Web UI bundle (zero user data)
COPY --chown=companion:companion harness.py .env.example LICENSE README.md ./
COPY --chown=companion:companion companion/ ./companion/
COPY --chown=companion:companion configs/ ./configs/
COPY --chown=companion:companion memory/ ./memory/
COPY --chown=companion:companion skills/ ./skills/
COPY --chown=companion:companion specs/ ./specs/
COPY --chown=companion:companion surfaces/ ./surfaces/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    COMPANION_IN_DOCKER=1 \
    COMPANION_IMMUTABLE_CORE=1 \
    COMPANION_SELF_DEV_LOCKED=1 \
    COMPANION_DATA_DIR=/data \
    COMPANION_BASE_URL=http://host.docker.internal:11434 \
    COMPANION_MODEL=qwen2.5:7b

VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request, sys; r = urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3); sys.exit(0 if r.status == 200 else 1)"

USER companion
CMD ["python", "harness.py", "--web", "--host", "0.0.0.0", "--port", "8000"]
