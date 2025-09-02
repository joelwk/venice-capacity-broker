.PHONY: help health create-tenant chat-admin limits-get limits-set

# Broker base URL; override in env or on the command line
# Example: make create-tenant BROKER_BASE_URL=https://<your-repl>.repl.co
BROKER_BASE_URL ?= $(shell python - <<'PY'
import os
base=os.getenv('BROKER_BASE_URL')
if base:
  print(base.rstrip('/'))
else:
  host=os.getenv('BROKER_API_HOST','127.0.0.1')
  port=os.getenv('BROKER_API_PORT','8000')
  print(f'http://{host}:{port}')
PY
)

help:
	@echo "Targets:"
	@echo "  make health                          - GET /health"
	@echo "  make create-tenant TENANT=t1 LABEL=TeamA [QUOTA=0 EXPIRES=...]"
	@echo "  make chat-admin TENANT=t1 [MESSAGE=Hello]    - admin act-as /v1/chat"
	@echo "  make limits-get TENANT=t1            - admin get broker limits"
	@echo "  make limits-set TENANT=t1 WINDOW=60 MAX=60 [LABEL=premium]"

health:
	@curl -fsS "$(BROKER_BASE_URL)/health" | jq . || curl -fsS "$(BROKER_BASE_URL)/health"

create-tenant:
	@if [ -z "$$BROKER_ADMIN_TOKEN" ]; then echo "BROKER_ADMIN_TOKEN env is required"; exit 1; fi
	@python scripts/create_test_tenant.py --tenant-id "$(TENANT)" --label "$(LABEL)" $(if $(QUOTA),--quota $(QUOTA),) $(if $(EXPIRES),--expires-at "$(EXPIRES)",)

chat-admin:
	@if [ -z "$(TENANT)" ]; then echo "Usage: make chat-admin TENANT=t1 [MESSAGE=Hello]"; exit 1; fi
	@if [ -z "$$BROKER_ADMIN_TOKEN" ]; then echo "BROKER_ADMIN_TOKEN env is required"; exit 1; fi
	@curl -sS -X POST "$(BROKER_BASE_URL)/v1/chat" \
	  -H "Authorization: Bearer $$BROKER_ADMIN_TOKEN" \
	  -H "X-Tenant-Id: $(TENANT)" \
	  -H "Content-Type: application/json" \
	  -d '{"messages":[{"role":"user","content":"$(or $(MESSAGE),Hello)"}]}' | jq . || true

limits-get:
	@if [ -z "$(TENANT)" ]; then echo "Usage: make limits-get TENANT=t1"; exit 1; fi
	@if [ -z "$$BROKER_ADMIN_TOKEN" ]; then echo "BROKER_ADMIN_TOKEN env is required"; exit 1; fi
	@curl -sS "$(BROKER_BASE_URL)/v1/tenants/$(TENANT)/broker-limits" -H "Authorization: Bearer $$BROKER_ADMIN_TOKEN" | jq . || true

limits-set:
	@if [ -z "$(TENANT)" ]; then echo "Usage: make limits-set TENANT=t1 WINDOW=60 MAX=60 [LABEL=premium]"; exit 1; fi
	@if [ -z "$$BROKER_ADMIN_TOKEN" ]; then echo "BROKER_ADMIN_TOKEN env is required"; exit 1; fi
	@python - <<'PY'
import os, json, sys
tenant=os.getenv('TENANT')
url=os.getenv('BROKER_BASE_URL').rstrip('/')+f"/v1/tenants/{tenant}/broker-limits"
payload={}
w=os.getenv('WINDOW'); m=os.getenv('MAX'); l=os.getenv('LABEL')
if w: payload['windowSeconds']=int(w)
if m: payload['maxRequests']=int(m)
if l: payload['label']=l
import requests
r=requests.post(url, headers={'Authorization': f"Bearer {os.getenv('BROKER_ADMIN_TOKEN')}", 'Content-Type':'application/json'}, json=payload, timeout=20)
print(r.status_code)
try: print(json.dumps(r.json(), indent=2))
except Exception: print(r.text)
PY

