# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# Builder stage — compiles/installs Python dependencies into an isolated
# virtualenv. Needs `build-essential`/`libpq-dev` for packages with native
# extensions; none of that (nor pip's build cache) ships in the runtime
# image below. Stage 8 — devops, PR11.
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS builder

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY pyproject.toml README.md ./
COPY app ./app

# Real immutable install (not `-e`) — the wheel's contents, not a live
# bind-mount of the source tree, is what ships in the runtime image.
RUN pip install --upgrade pip && pip install .

# ---------------------------------------------------------------------------
# Runtime stage — no compilers, no build deps. `postgresql-client` is
# required here (not `build-essential`) because PR8's backup worker shells
# out to `pg_dump` directly against the `postgres` service.
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system app \
    && useradd --system --gid app --home-dir /app --shell /usr/sbin/nologin app

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /app/app /app/app

RUN chown -R app:app /app

USER app

EXPOSE 8000

# Default entrypoint runs the API. `docker-compose.prod.yml`'s `worker`
# service overrides this command with
# `arq app.workers.settings.WorkerSettings` — same image, two entrypoints.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
