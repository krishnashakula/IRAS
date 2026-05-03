# syntax=docker/dockerfile:1.7
ARG PYTHON_VERSION=3.13
FROM python:${PYTHON_VERSION}-slim AS base

# Build arguments (injected by CI)
ARG BUILD_DATE
ARG VCS_REF
ARG VERSION=dev

LABEL org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.title="IRAS" \
      org.opencontainers.image.description="Autonomous Incident Response Agent System" \
      org.opencontainers.image.source="https://github.com/${GITHUB_REPOSITORY}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ─── Build stage: compile wheels ─────────────────────────────────────────────
FROM base AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

COPY pyproject.toml README.md ./
COPY src/ src/

RUN pip install --upgrade pip && \
    pip wheel --wheel-dir /wheels .

# ─── Runtime stage: minimal final image ──────────────────────────────────────
FROM base AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Non-root user for security
RUN useradd --create-home --shell /bin/bash --uid 1001 iras
WORKDIR /app
RUN chown iras:iras /app

COPY --from=builder /wheels /wheels
COPY --chown=iras:iras pyproject.toml README.md ./
COPY --chown=iras:iras src/ src/
COPY --chown=iras:iras run.py ./

RUN pip install --upgrade pip && \
    pip install --no-index --find-links=/wheels iras && \
    rm -rf /wheels

USER iras

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

CMD ["python", "run.py"]
