# syntax=docker/dockerfile:1
#
# Railway-optimised image for the ICT Silver Bullet bot.
# Slim base + a separate dependency layer so code-only pushes rebuild in seconds.

FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Europe/Tirane

WORKDIR /app

# Dependencies first: this layer is cached until requirements.txt changes.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Run as a non-root user; the state directory is the only writable path needed.
RUN useradd --create-home --uid 10001 trader \
    && mkdir -p /tmp/silver-bullet-state \
    && chown -R trader:trader /app /tmp/silver-bullet-state
USER trader

# Reports unhealthy when the MCP session is down. Only meaningful when PORT is
# set (Railway sets it for web services); a worker deploy passes trivially.
HEALTHCHECK --interval=60s --timeout=10s --start-period=45s --retries=3 \
    CMD ["python", "healthcheck.py"]

CMD ["python", "-u", "main.py"]
