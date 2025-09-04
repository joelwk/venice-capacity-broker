.PHONY: help health create-tenant chat-admin rotate-probe limits-get limits-set setup-cli run-broker db-migrate db-stamp db-compact db-counters demo-e2e enable-buyer
 .PHONY: db-setup-and-migrate

# Broker base URL; can be overridden via environment or CLI
# Example: make create-tenant BROKER_BASE_URL=https://<your-repl>.repl.co
BROKER_API_HOST ?= 127.0.0.1
BROKER_API_PORT ?= 8000
BROKER_BASE_URL ?= http://$(BROKER_API_HOST):$(BROKER_API_PORT)
# Trim any trailing slash from base URL to avoid 301 redirects
BASE_URL := $(patsubst %/,%, $(BROKER_BASE_URL))
MESSAGE ?= Hello
# Try to find uv in common install locations; fall back to plain python
UV_BIN ?= $(if $(wildcard $(HOME)/.local/bin/uv),$(HOME)/.local/bin/uv,$(if $(wildcard $(PWD)/.local/bin/uv),$(PWD)/.local/bin/uv,))
RUNPY ?= $(if $(UV_BIN),$(UV_BIN) run python,python)

setup-cli:
	@echo "Installing minimal CLI dependencies (requests + httpx) for shell usage..."
	@python -m pip install --upgrade pip setuptools wheel || true
	@python -m pip install requests httpx

.PHONY: env-status
env-status:
	@$(RUNPY) apps/cli/main.py env:status

	help:
	@echo "Targets:"
	@echo "  make health                          - GET /health"
	@echo "  make create-tenant TENANT=t1 LABEL=TeamA [QUOTA=0 EXPIRES=...]"
	@echo "  make chat-admin TENANT=t1 [MESSAGE=Hello]    - admin act-as /v1/chat"
	@echo "  make rotate-probe TENANT=t1 [LABEL=TeamA MESSAGE=Hello] - rotate subkey then probe /v1/chat"
	@echo "  make limits-get TENANT=t1            - admin get broker limits"
	@echo "  make limits-set TENANT=t1 WINDOW=60 MAX=60 [LABEL=premium]"
	@echo "  make run-broker                      - start uvicorn app:app with --app-dir"
	@echo "  make db-migrate                      - alembic upgrade head"
	@echo "  make db-stamp                        - alembic stamp head (mark current)"
	@echo "  make db-setup-and-migrate           - install DB deps + alembic, then upgrade head"
	@echo "  make db-compact                      - compact KV counters into SQL (force)"
	@echo "  make db-counters TENANT=t1 [LIMIT=20] - show recent counters from SQL"
	@echo "  make demo-e2e TENANT=t1 [LABEL=TeamA MESSAGE=Hello LIMIT=20 MODEL=<m>] - seed+probe+compact+show"
	@echo "  make enable-buyer                    - append Buyer feature flags to .env and print restart tips"
	@echo "  make watch-tokens                    - run BaseScan token watcher (requires BASESCAN_API_KEY)"

health:
	@curl -fsSL -H "Accept: application/json" "$(BASE_URL)/health"

create-tenant:
	@if [ -z "$$BROKER_ADMIN_TOKEN" ]; then echo "BROKER_ADMIN_TOKEN env is required"; exit 1; fi
	@$(RUNPY) scripts/create_test_tenant.py --tenant-id "$(TENANT)" --label "$(LABEL)" $(if $(QUOTA),--quota $(QUOTA),) $(if $(EXPIRES),--expires-at "$(EXPIRES)",) $(if $(ROTATE),--rotate,) $(if $(REVOKE_OLD),--revoke-old,) $(if $(PROBE_CHAT),--probe-chat,)

chat-admin:
	@if [ -z "$(TENANT)" ]; then echo "Usage: make chat-admin TENANT=t1 [MESSAGE=Hello]"; exit 1; fi
	@if [ -z "$$BROKER_ADMIN_TOKEN" ]; then echo "BROKER_ADMIN_TOKEN env is required"; exit 1; fi
	@PAY='{"messages":[{"role":"user","content":"$(MESSAGE)"}]'; \
  if [ -n "$(EFFECTIVE_MODEL)" ]; then PAY="$${PAY},\"model\":\"$(EFFECTIVE_MODEL)\""; fi; \
	  PAY="$${PAY}}"; \
	  curl -sSL -H "Accept: application/json" -X POST "$(BASE_URL)/v1/chat" \
	    -H "Authorization: Bearer $$BROKER_ADMIN_TOKEN" \
	    -H "X-Tenant-Id: $(TENANT)" \
	    -H "Content-Type: application/json" \
	    -H "Idempotency-Key: $${IDK:-idk-$$RANDOM-`date +%s`}" \
	    -d "$$PAY" \
	    -w "\nHTTP %{http_code}\n"

# Rotate subkey (using VENICE_PARENT_KEY/VENICE_API_KEY) then probe chat as admin act-as
.PHONY: rotate-probe
rotate-probe:
	@if [ -z "$(TENANT)" ]; then echo "Usage: make rotate-probe TENANT=t1 [LABEL=TeamA MESSAGE=Hello]"; exit 1; fi
	@if [ -z "$$BROKER_ADMIN_TOKEN" ]; then echo "BROKER_ADMIN_TOKEN env is required"; exit 1; fi
	@$(MAKE) -s create-tenant TENANT=$(TENANT) LABEL="$(if $(LABEL),$(LABEL),Team A)" ROTATE=1 REVOKE_OLD=1
	@$(MAKE) -s chat-admin TENANT=$(TENANT) MESSAGE="$(if $(MESSAGE),$(MESSAGE),Hello)"

limits-get:
	@if [ -z "$(TENANT)" ]; then echo "Usage: make limits-get TENANT=t1"; exit 1; fi
	@if [ -z "$$BROKER_ADMIN_TOKEN" ]; then echo "BROKER_ADMIN_TOKEN env is required"; exit 1; fi
	@curl -sSL -H "Accept: application/json" "$(BASE_URL)/v1/tenants/$(TENANT)/broker-limits" -H "Authorization: Bearer $$BROKER_ADMIN_TOKEN"

limits-set:
	@if [ -z "$(TENANT)" ]; then echo "Usage: make limits-set TENANT=t1 WINDOW=60 MAX=60 [LABEL=premium]"; exit 1; fi
	@if [ -z "$$BROKER_ADMIN_TOKEN" ]; then echo "BROKER_ADMIN_TOKEN env is required"; exit 1; fi
	@$(RUNPY) scripts/set_broker_limits.py --tenant "$(TENANT)" $(if $(WINDOW),--window $(WINDOW),) $(if $(MAX),--max $(MAX),) $(if $(LABEL),--label "$(LABEL)",)

# --- Convenience targets ---
run-broker:
	@if command -v uv >/dev/null 2>&1; then \
	  uv run uvicorn app:app --app-dir apps/broker-api --host 0.0.0.0 --port $(BROKER_API_PORT); \
	else \
	  python -m uvicorn app:app --app-dir apps/broker-api --host 0.0.0.0 --port $(BROKER_API_PORT); \
	fi

db-migrate:
	@$(RUNPY) -m alembic upgrade head

db-stamp:
	@$(RUNPY) -m alembic stamp head

# Install DB extras and Alembic, then upgrade head using the current Python runner
db-setup-and-migrate:
	@echo "Installing DB extras and Alembic, then running migrations..."
	@$(MAKE) -s setup-db
	@if command -v uv >/dev/null 2>&1; then \
	  uv sync --extra dev; \
	else \
	  python -m pip install --upgrade pip setuptools wheel || true; \
	  python -m pip install alembic; \
	fi
	@$(RUNPY) -m alembic upgrade head

db-compact:
	@$(RUNPY) apps/cli/main.py data:compact-counters --force

db-counters:
	@if [ -z "$(TENANT)" ]; then echo "Usage: make db-counters TENANT=t1 [LIMIT=20]"; exit 1; fi
	@$(RUNPY) apps/cli/main.py counters:show --tenant "$(TENANT)" --limit $(if $(LIMIT),$(LIMIT),20) --json

# In-process server compaction (admin-only). Useful when KV is in-memory or prefix listing is unavailable.
.PHONY: server-db-compact
server-db-compact:
	@if [ -z "$$BROKER_ADMIN_TOKEN" ]; then echo "BROKER_ADMIN_TOKEN env is required"; exit 1; fi
	@curl -sSL -X POST "$(BASE_URL)/v1/admin/compact-counters?minutes=$(if $(MINUTES),$(MINUTES),60)&delete_after=$(if $(DELETE_AFTER),$(DELETE_AFTER),false)" \
	  -H "Authorization: Bearer $$BROKER_ADMIN_TOKEN" -H "Accept: application/json"

# One-shot Replit demo: seed tenant (SQL if no Venice parent), probe chat, compact, show
TENANT ?= t1
LABEL ?= Team A
demo-e2e:
	@if [ -z "$$BROKER_ADMIN_TOKEN" ]; then echo "BROKER_ADMIN_TOKEN env is required"; exit 1; fi
	@echo "[demo] Seeding tenant '$(TENANT)' (label='$(LABEL)')"
	@if [ -n "$$FORCE_SQL" ]; then \
	  $(RUNPY) scripts/seed_sql_tenant.py --tenant "$(TENANT)" --label "$(LABEL)" || true; \
	elif [ -n "$$VENICE_PARENT_KEY" ] || [ -n "$$VENICE_API_KEY" ]; then \
	  $(RUNPY) scripts/create_test_tenant.py --tenant-id "$(TENANT)" --label "$(LABEL)" || true; \
	else \
	  $(RUNPY) scripts/seed_sql_tenant.py --tenant "$(TENANT)" --label "$(LABEL)" || true; \
	fi
	@echo "[demo] Probing /v1/chat as admin act-as (this also writes limiter buckets)"
	@$(MAKE) -s chat-admin TENANT=$(TENANT) MESSAGE="$(if $(MESSAGE),$(MESSAGE),Hello)" MODEL="$(if $(MODEL),$(MODEL),$(EFFECTIVE_MODEL))" || true
	@echo "[demo] Compacting KV -> SQL counters"
	@$(MAKE) -s server-db-compact || true
	@$(MAKE) -s db-compact || true
	@echo "[demo] Showing counters for $(TENANT)"
	@$(MAKE) -s db-counters TENANT=$(TENANT) LIMIT=$(if $(LIMIT),$(LIMIT),20)

.PHONY: watch-tokens
watch-tokens:
    @# Best-effort: load .env quietly if present; never fail if missing
    @set -a; ( [ -f .env ] && . .env ) >/dev/null 2>&1 || true; set +a; \
    if [ -z "$$BASESCAN_API_KEY" ] && [ -z "$$ETHERSCAN_API_KEY" ]; then echo "Set BASESCAN_API_KEY or ETHERSCAN_API_KEY"; exit 1; fi; \
    $(RUNPY) services/marketdata/token_watcher.py
# Pull defaults from .env/.env.example without overriding exported env
# These are used only for Make computations (e.g., default MODEL)
BROKER_DEFAULT_MODEL_FILE := $(strip $(shell awk -F= '/^BROKER_DEFAULT_MODEL[[:space:]]*=/{print $$2}' .env 2>/dev/null | tr -d '\r'))
ifeq ($(BROKER_DEFAULT_MODEL_FILE),)
  BROKER_DEFAULT_MODEL_FILE := $(strip $(shell awk -F= '/^BROKER_DEFAULT_MODEL[[:space:]]*=/{print $$2}' .env.example 2>/dev/null | tr -d '\r'))
endif
VENICE_DEFAULT_MODEL_FILE := $(strip $(shell awk -F= '/^VENICE_DEFAULT_MODEL[[:space:]]*=/{print $$2}' .env 2>/dev/null | tr -d '\r'))
ifeq ($(VENICE_DEFAULT_MODEL_FILE),)
  VENICE_DEFAULT_MODEL_FILE := $(strip $(shell awk -F= '/^VENICE_DEFAULT_MODEL[[:space:]]*=/{print $$2}' .env.example 2>/dev/null | tr -d '\r'))
endif

# Effective model preference: explicit MODEL > BROKER_DEFAULT_MODEL > VENICE_DEFAULT_MODEL > file fallbacks
EFFECTIVE_MODEL := $(firstword $(strip $(MODEL) $(BROKER_DEFAULT_MODEL) $(VENICE_DEFAULT_MODEL) $(BROKER_DEFAULT_MODEL_FILE) $(VENICE_DEFAULT_MODEL_FILE)))

# --- Setup helpers ---
.PHONY: setup-db
setup-db:
	@echo "Installing SQL extras (sqlmodel + psycopg2-binary)..."
	@if command -v uv >/dev/null 2>&1; then \
	  uv sync --extra db; \
	else \
	  python -m pip install --upgrade pip setuptools wheel || true; \
	  python -m pip install sqlmodel psycopg2-binary; \
	fi

.PHONY: enable-buyer
enable-buyer:
	@echo "Enabling Buyer features by appending flags to .env ..."
	@touch .env
	@{
	  echo "# --- Buyer features (generated by make enable-buyer) ---";
	  echo "QUOTES_ENABLED=true";
	  echo "PURCHASES_ENABLED=true";
	  echo "CORS_ENABLED=true";
	  echo "ACCEPT_ASSETS=$${ACCEPT_ASSETS:-ETH,USDC}";
	  echo "PRICE_UNIT_USDC=$${PRICE_UNIT_USDC:-100000}";
	  echo "PRICE_QUOTE_TTL_SECONDS=$${PRICE_QUOTE_TTL_SECONDS:-120}";
	} >> .env
	@echo "Flags appended to .env. Set CORS_ALLOW_ORIGINS, TREASURY_ADDRESS, and USDC_ADDRESS as needed."
	@echo "Restart the broker process to apply changes. On Replit, stop and run again."
