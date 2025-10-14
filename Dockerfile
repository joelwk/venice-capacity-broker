FROM python:3.11-slim

# Environment
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Minimal system deps
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl && rm -rf /var/lib/apt/lists/*

# Project files needed for install/runtime
COPY pyproject.toml README.md alembic.ini ./
COPY apps ./apps
COPY libs ./libs
COPY services ./services
COPY db ./db
COPY abi ./abi
COPY docs ./docs

# Install package with required extras
RUN pip install --no-cache-dir .[agentkit,broker,web3,graph,db]

EXPOSE 8000

# Run migrations on boot, then start API
CMD bash -lc "python -m alembic upgrade head || true; exec uvicorn app:app --app-dir apps/broker-api --host 0.0.0.0 --port 8000"
