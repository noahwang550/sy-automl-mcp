# Multi-tier Dockerfile for sy-automl-mcp.
# Base image is Linux — AutoGluon is officially supported on Linux only, so the
# server runs in a container regardless of host OS (Windows host uses Docker Desktop).
#
# Build tiers (build arg TIER):
#   tabular (default) -> autogluon.tabular only (~smaller, CPU-friendly, MVP)
#   full              -> + autogluon.timeseries + autogluon.multimodal (heavier, GPU optional)
#
# Build:
#   docker build -t sy-automl-mcp .
#   docker build -t sy-automl-mcp:full --build-arg TIER=full .
#
# Run (stdio, for local Claude Code):
#   docker run -i --rm -v "$PWD/artifacts:/app/artifacts" sy-automl-mcp
#
# Run (streamable-http, remote/shared):
#   docker run --rm -p 8000:8000 -e MCP_TRANSPORT=http -e MCP_PORT=8000 \
#     -v "$PWD/artifacts:/app/artifacts" sy-automl-mcp

ARG PYTHON_VERSION=3.11
FROM python:${PYTHON_VERSION}-slim AS base

# AutoGluon needs compilers and a few system libs for its native deps.
# torch/torchvision (multimodal) additionally want libgomp and GL libs.
ARG TIER=tabular
ENV TIER=${TIER} \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
    PIP_TRUSTED_HOST=pypi.tuna.tsinghua.edu.cn \
    PIP_DEFAULT_TIMEOUT=300 \
    PIP_RETRIES=10

# Swap apt sources to Aliyun mirror (host is behind GFW; deb.debian.org times out).
RUN sed -i 's|deb.debian.org|mirrors.aliyun.com|g; s|security.debian.org|mirrors.aliyun.com|g' \
        /etc/apt/sources.list.d/debian.sources 2>/dev/null \
    || sed -i 's|deb.debian.org|mirrors.aliyun.com|g; s|security.debian.org|mirrors.aliyun.com|g' \
        /etc/apt/sources.list 2>/dev/null || true

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        gcc \
        g++ \
        git \
        curl \
        libgomp1 \
        libgl1 \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install dependencies first (better layer caching).
COPY requirements.txt requirements-full.txt ./
RUN if [ "$TIER" = "full" ]; then \
        pip install -r requirements-full.txt; \
    else \
        pip install -r requirements.txt; \
    fi

# Copy application code.
COPY server.py config.py ./
COPY tools/ ./tools/
COPY tasks/ ./tasks/
COPY serialization/ ./serialization/

# License compliance: bundle the project license, NOTICE, and the full
# third-party attribution list into the distributed image (Apache-2.0 / MIT /
# BSD-3-Clause deps require license text + attribution in redistributions).
COPY LICENSE NOTICE THIRD_PARTY_LICENSES.txt /app/
COPY scripts/gen_licenses.py ./scripts/

RUN mkdir -p /app/artifacts/datasets /app/artifacts/models /app/artifacts/predictions /app/artifacts/logs
ENV ARTIFACTS_DIR=/app/artifacts

# Default transport is stdio (local Claude Code). Override with MCP_TRANSPORT=http.
ENV MCP_TRANSPORT=stdio
ENTRYPOINT ["python", "server.py"]
