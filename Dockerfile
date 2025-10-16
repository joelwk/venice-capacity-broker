FROM python:3.11-slim

# Environment
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Minimal system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    libsqlite3-0 \
    sqlite3 \
 && rm -rf /var/lib/apt/lists/*

# Project files needed for install/runtime
COPY pyproject.toml README.md alembic.ini ./
COPY docker-compose.yml ./docker-compose.yml
COPY apps ./apps
COPY libs ./libs
COPY services ./services
COPY db ./db
COPY abi ./abi
COPY docs ./docs
COPY scripts ./scripts
COPY config ./config
COPY agents ./agents
COPY graph ./graph
COPY tests ./tests

# Install package with required extras
ENV PYTHONPATH=/app
RUN pip install --no-cache-dir .[agentkit,broker,web3,graph,db] && \
    pip install --no-cache-dir pytest

RUN chmod +x /app/scripts/docker_start_broker.sh

EXPOSE 8000

# Run validation, migrations, tests, then start API
CMD ["/app/scripts/docker_start_broker.sh"]
