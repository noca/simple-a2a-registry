# =============================================================================
# Dockerfile — Simple A2A Registry (multi-stage build)
#
# Build stage:   pip install + npm build
# Runtime stage: python slim scratch
#
# NOTE: ARG name PY_TAG_VERSION avoids collision with the base image's
# PYTHON_VERSION ENV (e.g. python:3.11-slim sets PYTHON_VERSION=3.11.15).
# =============================================================================
ARG PY_TAG_VERSION=3.11
ARG NODE_VERSION=20

# ---------------------------------------------------------------------------
# Stage 1: Build — install Python deps + build frontend assets
# ---------------------------------------------------------------------------
FROM python:${PY_TAG_VERSION}-slim AS build

ARG PY_TAG_VERSION

# Compute short Python major.minor so COPY works regardless of patch level
RUN python3 -c "import sys; v=f'{sys.version_info.major}.{sys.version_info.minor}'; open('/py_ver', 'w').write(v)"

# Install build tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    pkg-config \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Python dependencies — copy only requirements for layer caching
COPY pyproject.toml README.md ./
COPY simple_a2a_registry/ simple_a2a_registry/
COPY migrations/ migrations/

RUN pip install --no-cache-dir --upgrade pip setuptools wheel && \
    pip install --no-cache-dir .

# Node.js for frontend build
FROM node:${NODE_VERSION}-bookworm-slim AS frontend-build

WORKDIR /build

COPY a2a-admin/package.json a2a-admin/package-lock.json ./a2a-admin/
RUN cd a2a-admin && npm ci

COPY a2a-admin/ ./a2a-admin/
RUN cd a2a-admin && npm run build

# ---------------------------------------------------------------------------
# Stage 2: Runtime — minimal python-slim image
# ---------------------------------------------------------------------------
FROM python:${PY_TAG_VERSION}-slim AS runtime

LABEL maintainer="NousResearch <dev@nousresearch.com>"
LABEL description="Simple A2A Registry — lightweight Agent-to-Agent registry server"

# Copy the version marker for dynamic lib-path resolution
COPY --from=build /py_ver /tmp/py_ver

# Install runtime dependencies (no build tools needed)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Create a non-root user
RUN groupadd -r a2a && useradd -r -g a2a -d /app -s /bin/false a2a

WORKDIR /app

# Copy site-packages from build stage (dynamically resolved version)
RUN PY_VER=$(cat /tmp/py_ver); \
    mkdir -p /usr/local/lib/python${PY_VER}/site-packages
COPY --from=build /usr/local/lib/python*/site-packages /tmp/build-pkgs/
RUN PY_VER=$(cat /tmp/py_ver); \
    cp -r /tmp/build-pkgs/* /usr/local/lib/python${PY_VER}/site-packages/ && \
    rm -rf /tmp/build-pkgs /tmp/py_ver

# Copy installed binaries
COPY --from=build /usr/local/bin /usr/local/bin

# Copy application code
COPY pyproject.toml README.md ./
COPY simple_a2a_registry/ simple_a2a_registry/
COPY scripts/ scripts/
COPY migrations/ migrations/

# Copy pre-built frontend assets
COPY --from=frontend-build /build/data/web ./data/web

# Data directory for runtime persistence
RUN mkdir -p /app/data && chown -R a2a:a2a /app/data

# Health check (Kubernetes-friendly)
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8321/health', timeout=5)" || exit 1

USER a2a

EXPOSE 8321

# Default: run production server
# Override CMD to pass additional flags, e.g.:
#   docker run ... image --auth-enabled --bootstrap-secret mysecret
ENTRYPOINT ["python", "-m", "simple_a2a_registry"]
CMD ["server", "--host", "0.0.0.0", "--port", "8321", "--data-dir", "/app/data", "--log-level", "INFO", "--log-format", "json"]