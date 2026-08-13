FROM python:3.11-slim

# Set working directory
WORKDIR /app

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Python deps
COPY pyproject.toml poetry.lock* requirements.txt* /app/
RUN set -eux; \
	if [ -f requirements.txt ]; then \
		pip install --no-cache-dir -r requirements.txt; \
	elif [ -f pyproject.toml ]; then \
		pip install --no-cache-dir poetry && python -m poetry config virtualenvs.create false && python -m poetry install --no-interaction --no-ansi; \
	else \
		echo "No requirements.txt or pyproject.toml found" >&2; exit 1; \
	fi

# App
COPY . /app

# Expose default port
EXPOSE 8000

# Default environment
ENV APP_ENV=production \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

# Prestart and entrypoint
RUN sed -i 's/\r$//' scripts/prestart.sh scripts/docker_entrypoint.sh scripts/print_env_diagnostics.sh \
    && chmod +x scripts/prestart.sh scripts/docker_entrypoint.sh

ENTRYPOINT ["/bin/sh","scripts/docker_entrypoint.sh"]
CMD ["uvicorn","apps.broker_api.app:app","--host","0.0.0.0","--port","8000"]
