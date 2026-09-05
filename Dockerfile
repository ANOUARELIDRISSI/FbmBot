# Base Python image + `playwright install --with-deps` rather than pinning
# to a specific mcr.microsoft.com/playwright image tag — more robust since
# it resolves the correct OS packages for whatever Chromium version our
# pinned `playwright` Python package (see uv.lock) actually needs, instead
# of hoping a matching pre-built image tag exists.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    TZ=Europe/Brussels \
    UV_LINK_MODE=copy

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app

# Dependencies first, in their own layer — code changes shouldn't bust the
# (slow) Playwright/Chromium install cache.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

# --no-project: source isn't copied into the image yet at this point (by
# design, so editing source doesn't bust this slow layer's cache) — a
# plain `uv run` would otherwise try to sync/build the becarscout project
# itself and fail with "Expected a Python module at src/becarscout/__init__.py".
RUN uv run --no-project playwright install --with-deps chromium

# cron for the hourly schedule (see docker/crontab); the pipeline itself
# stays plain Python — cron is just what triggers `becarscout run` on time.
RUN apt-get update \
    && apt-get install -y --no-install-recommends cron \
    && rm -rf /var/lib/apt/lists/*

COPY . .
RUN uv sync --frozen

# Stage 7's feedback agent (see feedback_agent/memory.py) embeds locally via
# FastEmbed, which otherwise downloads its ~130MB ONNX model from Hugging
# Face the first time anything calls it at runtime — baked into the image
# here instead, same reasoning as installing Chromium at build time above
# rather than on the container's first real scrape.
RUN uv run python -c "from fastembed import TextEmbedding; TextEmbedding('BAAI/bge-small-en-v1.5')"

COPY docker/crontab /etc/cron.d/becarscout-cron
RUN chmod 0644 /etc/cron.d/becarscout-cron \
    && crontab /etc/cron.d/becarscout-cron \
    && chmod +x docker/entrypoint.sh docker/run_pipeline.sh

ENTRYPOINT ["docker/entrypoint.sh"]
