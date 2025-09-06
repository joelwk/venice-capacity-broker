Resolved 140 packages in 0.85ms
Audited 138 packages in 1ms
2025-09-06 17:27:08,516 | INFO | broker.api | admin ui: mounted at /admin from /home/runner/workspace/apps/control-plane
2025-09-06 17:27:09,517 | INFO | broker.api | broker.store: using SQL backend
2025-09-06 17:27:09,517 | INFO | broker.api | security: admin token configured; admin endpoints require bearer token
2025-09-06 17:27:09,519 | INFO | broker.api | rate-limiter: enabled (window=60s, max=60)
2025-09-06 17:27:09,545 | INFO | broker.api | metrics: starlette_exporter not installed; falling back to builtin
INFO:     Started server process [1367]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on http://0.0.0.0:5000 (Press CTRL+C to quit)
INFO:     172.31.64.162:38634 - "GET /admin/ HTTP/1.1" 304 Not Modified
INFO:     172.31.64.162:38634 - "GET /admin/app.js HTTP/1.1" 304 Not Modified
INFO:     172.31.64.162:38634 - "GET /health HTTP/1.1" 200 OK
INFO:     172.31.64.162:38644 - "GET /v1/env HTTP/1.1" 200 OK
INFO:     172.31.64.162:38652 - "GET /v1/admin/quotes HTTP/1.1" 200 OK
INFO:     172.31.64.162:38634 - "GET /v1/tenants HTTP/1.1" 200 OK
INFO:     172.31.64.162:38660 - "GET /v1/admin/purchases HTTP/1.1" 200 OK
INFO:     172.31.64.162:38644 - "GET /v1/env HTTP/1.1" 200 OK
INFO:     172.31.64.162:38634 - "GET /v1/admin/quotes?limit=50 HTTP/1.1" 401 Unauthorized
INFO:     172.31.64.162:38652 - "GET /v1/admin/quotes HTTP/1.1" 200 OK
INFO:     172.31.64.162:38652 - "GET /v1/admin/purchases?limit=50 HTTP/1.1" 401 Unauthorized
INFO:     172.31.64.162:38652 - "GET /v1/admin/purchases HTTP/1.1" 200 OK
INFO:     172.31.64.162:38652 - "GET /v1/admin/purchases?limit=50 HTTP/1.1" 401 Unauthorized
INFO:     172.31.64.162:38634 - "GET /v1/admin/purchases HTTP/1.1" 200 OK
INFO:     172.31.64.162:38634 - "GET /v1/market/tokens HTTP/1.1" 200 OK
INFO:     172.31.64.162:47858 - "GET / HTTP/1.1" 200 OK
INFO:     172.31.64.162:47858 - "GET /docs HTTP/1.1" 200 OK
INFO:     172.31.64.162:47858 - "GET /openapi.json HTTP/1.1" 200 OK
INFO:     172.31.64.162:47858 - "GET /docs/admin HTTP/1.1" 404 Not Found
INFO:     172.31.64.162:47858 - "GET /admin HTTP/1.1" 307 Temporary Redirect
INFO:     172.31.64.162:58184 - "GET / HTTP/1.1" 200 OK
INFO:     172.31.64.162:58184 - "GET /admin HTTP/1.1" 307 Temporary Redirect
INFO:     172.31.64.162:58184 - "GET /admin/ HTTP/1.1" 304 Not Modified
INFO:     172.31.64.162:58184 - "GET /admin/app.js HTTP/1.1" 304 Not Modified
INFO:     172.31.64.162:58184 - "GET /health HTTP/1.1" 200 OK
INFO:     172.31.64.162:60654 - "GET /v1/env HTTP/1.1" 200 OK
INFO:     172.31.64.162:60650 - "GET /v1/admin/quotes HTTP/1.1" 200 OK
INFO:     172.31.64.162:58184 - "GET /v1/tenants HTTP/1.1" 200 OK
INFO:     172.31.64.162:60652 - "GET /v1/admin/purchases HTTP/1.1" 200 OK
INFO:     172.31.64.162:60654 - "GET /v1/env HTTP/1.1" 200 OK
INFO:     172.31.64.162:58184 - "GET /v1/admin/purchases?limit=50 HTTP/1.1" 401 Unauthorized
INFO:     172.31.64.162:58184 - "GET /v1/admin/purchases HTTP/1.1" 200 OK
INFO:     172.31.64.162:60650 - "GET /v1/admin/quotes?limit=50 HTTP/1.1" 401 Unauthorized
INFO:     172.31.64.162:58184 - "GET /v1/admin/quotes HTTP/1.1" 200 OK
INFO:     172.31.64.162:58184 - "GET /v1/market/tokens HTTP/1.1" 200 OK
INFO:     172.31.64.162:41600 - "GET /v1/admin/purchases?limit=50 HTTP/1.1" 401 Unauthorized
INFO:     172.31.64.162:41596 - "GET /v1/admin/purchases HTTP/1.1" 200 OK
INFO:     172.31.64.162:53576 - "GET / HTTP/1.1" 200 OK
INFO:     172.31.64.162:53576 - "GET /docs HTTP/1.1" 200 OK
INFO:     172.31.64.162:53576 - "GET /openapi.json HTTP/1.1" 200 OK
INFO:     172.31.64.162:40384 - "GET /metrics HTTP/1.1" 200 OK
INFO:     172.31.64.162:43198 - "GET /v1/market/signals?ttl_s=30 HTTP/1.1" 502 Bad Gateway
INFO:     172.31.64.162:44470 - "GET /v1/market/prices?symbols=VVV%2CDIEM%2CETH%2CUSDC HTTP/1.1" 200 OK
INFO:     172.31.64.162:44470 - "GET /v1/market/prices?symbols=VVV%2CDIEM%2CETH%2CUSDC HTTP/1.1" 200 OK
INFO:     172.31.64.162:39516 - "GET /v1/admin/quotes?limit=50 HTTP/1.1" 401 Unauthorized
INFO:     172.31.64.162:39516 - "GET /v1/admin/quotes?limit=50 HTTP/1.1" 401 Unauthorized
INFO:     172.31.64.162:51954 - "GET /v1/admin/quotes?limit=50 HTTP/1.1" 401 Unauthorized
INFO:     172.31.64.162:41830 - "GET /v1/admin/quotes?limit=50 HTTP/1.1" 401 Unauthorized
INFO:     172.31.64.162:54584 - "GET /v1/market/signals?ttl_s=30 HTTP/1.1" 502 Bad Gateway
INFO:     172.31.64.162:54584 - "GET /v1/market/prices?symbols=VVV%2CDIEM%2CETH%2CUSDC HTTP/1.1" 200 OK
INFO:     172.31.64.162:35030 - "GET /v1/market/tokens HTTP/1.1" 200 OK
INFO:     172.31.64.162:47644 - "POST /v1/chat HTTP/1.1" 401 Unauthorized
INFO:     172.31.64.162:47686 - "POST /v1/chat HTTP/1.1" 409 Conflict
INFO:     172.31.64.162:33090 - "GET /v1/admin/venice/probe?timeout=10 HTTP/1.1" 401 Unauthorized
INFO:     172.31.64.162:33090 - "GET /v1/admin/venice/probe?timeout=10 HTTP/1.1" 401 Unauthorized
INFO:     172.31.64.162:60064 - "GET /v1/admin/venice/probe?base=https%3A%2F%2Fapi.venice.ai%2Fapi%2Fv1&timeout=10 HTTP/1.1" 401 Unauthorized
INFO:     172.31.64.162:45150 - "GET /v1/admin/venice/probe?base=https%3A%2F%2Fapi.venice.ai%2Fapi%2Fv1&timeout=10 HTTP/1.1" 401 Unauthorized
INFO:     172.31.64.162:47200 - "GET /v1/admin/venice/probe?base=https%3A%2F%2Fapi.venice.ai%2Fapi&timeout=10 HTTP/1.1" 401 Unauthorized




~/workspace$ uv run python apps/cli/main.py quotes:preview --units 1000000000000000000
  File "/home/runner/workspace/apps/cli/main.py", line 755
    sp = sub.add_parser("venice:signals", help="Fetch Venice VVV/DIEM tokenomic signals")
                                                                                         ^
IndentationError: unindent does not match any outer indentation level
~/workspace$ ^C
~/workspace$ uv run python apps/cli/main.py venice:signals
  File "/home/runner/workspace/apps/cli/main.py", line 755
    sp = sub.add_parser("venice:signals", help="Fetch Venice VVV/DIEM tokenomic signals")
                                                                                         ^
IndentationError: unindent does not match any outer indentation level
~/workspace$ uv run python apps/cli/main.py venice:signals
  File "/home/runner/workspace/apps/cli/main.py", line 755
    sp = sub.add_parser("venice:signals", help="Fetch Venice VVV/DIEM tokenomic signals")
                                                                                         ^
IndentationError: unindent does not match any outer indentation level
~/workspace$ uv run python apps/cli/main.py venice:probe-openapi --base-url https://api.venice.ai
  File "/home/runner/workspace/apps/cli/main.py", line 755
    sp = sub.add_parser("venice:signals", help="Fetch Venice VVV/DIEM tokenomic signals")
                                                                                         ^
IndentationError: unindent does not match any outer indentation level
~/workspace$ 








# HELP vvv_requests_total Total HTTP requests.
# TYPE vvv_requests_total counter
vvv_requests_total 40
# HELP vvv_errors_total 5xx HTTP responses.
# TYPE vvv_errors_total counter
vvv_errors_total 0
# HELP vvv_request_latency_seconds_sum Cumulative request latency in seconds.
# TYPE vvv_request_latency_seconds_sum counter
vvv_request_latency_seconds_sum 13.767805
# HELP vvv_requests_by_path_total Requests by path.
# TYPE vvv_requests_by_path_total counter
vvv_requests_by_path_total{path="/"} 3
vvv_requests_by_path_total{path="/admin"} 2
vvv_requests_by_path_total{path="/admin/"} 2
vvv_requests_by_path_total{path="/admin/app.js"} 2
vvv_requests_by_path_total{path="/docs"} 2
vvv_requests_by_path_total{path="/docs/admin"} 1
vvv_requests_by_path_total{path="/health"} 2
vvv_requests_by_path_total{path="/openapi.json"} 2
vvv_requests_by_path_total{path="/v1/admin/purchases"} 10
vvv_requests_by_path_total{path="/v1/admin/quotes"} 6
vvv_requests_by_path_total{path="/v1/env"} 4
vvv_requests_by_path_total{path="/v1/market/tokens"} 2
vvv_requests_by_path_total{path="/v1/tenants"} 2




url -X 'GET' \
  'https://06f39259-9a4c-45a1-88df-57b9ed34b438-00-6x87jshvwn29.worf.replit.dev/v1/market/signals?ttl_s=30' \
  -H 'accept: application/json'
Request URL
https://06f39259-9a4c-45a1-88df-57b9ed34b438-00-6x87jshvwn29.worf.replit.dev/v1/market/signals?ttl_s=30
Server response
Code	Details
502
Undocumented
Error: Bad Gateway

Response body
Download
{
  "detail": "Failed to fetch VVV signals: Venice error 404: {\"error\":\"Not found\"}. Hint: ensure VENICE_API_BASE_URL includes '/api/v1' (got 'https://api.venice.ai/api/v1'), or override VENICE_VVV_PATH / VENICE_DIEM_PATH and key paths as needed."
}
Response headers
 content-length: 248 
 content-type: application/json 
 date: Sat,06 Sep 2025 17:31:20 GMT 
 replit-cluster: worf 
 server: uvicorn 
 x-robots-tag: none,noindex,noarchive,nofollow,nositelinkssearchbox,noimageindex,none,noindex,noarchive,nofollow,nositelinkssearchbox,noimageindex 




 curl -X 'GET' \
  'https://06f39259-9a4c-45a1-88df-57b9ed34b438-00-6x87jshvwn29.worf.replit.dev/v1/market/signals?ttl_s=30' \
  -H 'accept: application/json'
Request URL
https://06f39259-9a4c-45a1-88df-57b9ed34b438-00-6x87jshvwn29.worf.replit.dev/v1/market/signals?ttl_s=30
Server response
Code	Details
502
Undocumented
Error: Bad Gateway

Response body
Download
{
  "detail": "Failed to fetch VVV signals: Venice error 404: {\"error\":\"Not found\"}. Hint: ensure VENICE_API_BASE_URL includes '/api/v1' (got 'https://api.venice.ai/api/v1'), or override VENICE_VVV_PATH / VENICE_DIEM_PATH and key paths as needed."
}
Response headers
 content-length: 248 
 content-type: application/json 
 date: Sat,06 Sep 2025 17:33:58 GMT 
 replit-cluster: worf 
 server: uvicorn 
 x-robots-tag: none,noindex,noarchive,nofollow,nositelinkssearchbox,noimageindex,none,noindex,noarchive,nofollow,nositelinkssearchbox,noimageindex 
Responses
Code	Description	Links
200	
Successful Response

Media type

application/json
Controls Accept header.
Example Value
Schema
{
  "additionalProp1": {}
}
No links
422	
Validation Error

Media type

application/json
Example Value
Schema
{
  "detail": [
    {
      "loc": [
        "string",
        0
      ],
      "msg": "string",
      "type": "string"
    }
  ]