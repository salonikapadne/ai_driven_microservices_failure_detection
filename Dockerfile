# Dockerfile — AI Failure Detection Agent (LangGraph)
#
# Static Docker CLI: guarantees `docker` on PATH (fixes runtime `docker: not found`).
# docker-compose (v1) from Debian: rollback/scale tools call `docker-compose` (not in static .tgz).
FROM python:3.11-slim-bookworm

ARG DOCKER_STATIC_VERSION=24.0.9
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && ARCH=$(dpkg --print-architecture) \
    && case "$ARCH" in \
         amd64) DARCH=x86_64 ;; \
         arm64) DARCH=aarch64 ;; \
         *) echo "unsupported dpkg arch: $ARCH" >&2; exit 1 ;; \
       esac \
    && curl -fsSL "https://download.docker.com/linux/static/stable/${DARCH}/docker-${DOCKER_STATIC_VERSION}.tgz" \
         | tar -xz -C /tmp \
    && install -m 755 /tmp/docker/docker /usr/local/bin/docker \
    && rm -rf /tmp/docker \
    && apt-get purge -y curl \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/* \
    && /usr/local/bin/docker --version

RUN apt-get update \
    && apt-get install -y --no-install-recommends docker-compose \
    && rm -rf /var/lib/apt/lists/* \
    && docker-compose --version

ENV PATH="/usr/local/bin:${PATH}"

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY ai_engine/ ./ai_engine/
COPY simulate.py .
COPY consumer.py .
COPY dashboard.py .
COPY tests/ ./tests/

CMD ["python", "dashboard.py"]
