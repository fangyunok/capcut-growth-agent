FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    GROWTH_RUNS_DIR=/app/runs \
    GROWTH_QWEN_BASE=http://host.docker.internal:11434/v1 \
    GROWTH_QWEN_MODEL=qwen3:4b-instruct

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/build
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN python -m pip install --no-cache-dir . \
    && useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/runs \
    && chown -R appuser:appuser /app

WORKDIR /app
USER appuser
EXPOSE 7860
CMD ["growth-agent", "serve", "--host", "0.0.0.0", "--port", "7860"]
