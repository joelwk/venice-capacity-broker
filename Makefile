.PHONY: help health create-tenant chat-admin limits-get limits-set setup-cli

# Broker base URL; can be overridden via environment or CLI
# Example: make create-tenant BROKER_BASE_URL=https://<your-repl>.repl.co
BROKER_API_HOST ?= 127.0.0.1
BROKER_API_PORT ?= 8000
BROKER_BASE_URL ?= http://$(BROKER_API_HOST):$(BROKER_API_PORT)
MESSAGE ?= Hello
# Try to find uv in common install locations; fall back to plain python
UV_BIN ?= $(if $(wildcard $(HOME)/.local/bin/uv),$(HOME)/.local/bin/uv,$(if $(wildcard $(PWD)/.local/bin/uv),$(PWD)/.local/bin/uv,))
RUNPY ?= $(if $(UV_BIN),$(UV_BIN) run python,python)

setup-cli:
	@echo "Installing CLI dependencies (requirements.txt) for shell usage..."
	@python -m pip install -r requirements.txt

help:
	@echo "Targets:"
	@echo "  make health                          - GET /health"
	@echo "  make create-tenant TENANT=t1 LABEL=TeamA [QUOTA=0 EXPIRES=...]"
	@echo "  make chat-admin TENANT=t1 [MESSAGE=Hello]    - admin act-as /v1/chat"
	@echo "  make limits-get TENANT=t1            - admin get broker limits"
	@echo "  make limits-set TENANT=t1 WINDOW=60 MAX=60 [LABEL=premium]"

health:
	@curl -fsSL -H "Accept: application/json" "$(BROKER_BASE_URL)/health"

create-tenant:
	@if [ -z "$$BROKER_ADMIN_TOKEN" ]; then echo "BROKER_ADMIN_TOKEN env is required"; exit 1; fi
	@$(RUNPY) scripts/create_test_tenant.py --tenant-id "$(TENANT)" --label "$(LABEL)" $(if $(QUOTA),--quota $(QUOTA),) $(if $(EXPIRES),--expires-at "$(EXPIRES)",)

chat-admin:
	@if [ -z "$(TENANT)" ]; then echo "Usage: make chat-admin TENANT=t1 [MESSAGE=Hello]"; exit 1; fi
	@if [ -z "$$BROKER_ADMIN_TOKEN" ]; then echo "BROKER_ADMIN_TOKEN env is required"; exit 1; fi
	@curl -sS -X POST "$(BROKER_BASE_URL)/v1/chat" \
	  -H "Authorization: Bearer $$BROKER_ADMIN_TOKEN" \
	  -H "X-Tenant-Id: $(TENANT)" \
	  -H "Content-Type: application/json" \
	  -d '{"messages":[{"role":"user","content":"$(MESSAGE)"}]}'

limits-get:
	@if [ -z "$(TENANT)" ]; then echo "Usage: make limits-get TENANT=t1"; exit 1; fi
	@if [ -z "$$BROKER_ADMIN_TOKEN" ]; then echo "BROKER_ADMIN_TOKEN env is required"; exit 1; fi
	@curl -sS "$(BROKER_BASE_URL)/v1/tenants/$(TENANT)/broker-limits" -H "Authorization: Bearer $$BROKER_ADMIN_TOKEN"

limits-set:
	@if [ -z "$(TENANT)" ]; then echo "Usage: make limits-set TENANT=t1 WINDOW=60 MAX=60 [LABEL=premium]"; exit 1; fi
	@if [ -z "$$BROKER_ADMIN_TOKEN" ]; then echo "BROKER_ADMIN_TOKEN env is required"; exit 1; fi
	@$(RUNPY) scripts/set_broker_limits.py --tenant "$(TENANT)" $(if $(WINDOW),--window $(WINDOW),) $(if $(MAX),--max $(MAX),) $(if $(LABEL),--label "$(LABEL)",)
