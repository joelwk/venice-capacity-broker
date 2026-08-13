# Docker Venice API Diagnostics Guide

This guide helps diagnose Venice API connectivity and API key propagation issues in Docker containers.

## Quick Diagnostic

Run the diagnostic script from your host machine:

```bash
# For orchestrator container (default)
bash scripts/docker_venice_diagnose.sh

# For a specific container
bash scripts/docker_venice_diagnose.sh <container-name>
```

Or run the Python diagnostic directly inside a container:

```bash
docker exec -it <container-name> python scripts/diagnose_docker_venice.py
```

## Manual Checks

### 1. Verify API Key Propagation

Check if `VENICE_API_KEY` is available in the container:

```bash
docker exec <container-name> sh -c 'echo "API Key: $([ -n "$VENICE_API_KEY" ] && echo "SET (${#VENICE_API_KEY} chars)" || echo "MISSING")"'
```

**Expected**: `API Key: SET (XX chars)`

**If missing**:
- Check that `.env`, `.env.docker`, or `docker/.env.local` contains `VENICE_API_KEY=...`
- Verify `docker-compose.yml` includes these files in `env_file:` section
- Ensure the key is not commented out or malformed

### 2. Verify Base URL Configuration

```bash
docker exec <container-name> sh -c 'echo "Base URL: ${VENICE_API_BASE_URL:-NOT SET}"'
```

**Expected**: `Base URL: https://api.venice.ai/api/v1`

**If incorrect**:
- Check `docker-compose.yml` has `VENICE_API_BASE_URL: https://api.venice.ai/api/v1`
- Ensure it includes the `/api/v1` suffix

### 3. Test DNS Resolution

```bash
docker exec <container-name> nslookup api.venice.ai
# or
docker exec <container-name> getent hosts api.venice.ai
```

**Expected**: Returns IP address(es) for `api.venice.ai`

**If failed**:
- Check Docker network configuration
- Verify container can reach external DNS servers
- Test with `docker exec <container-name> ping -c 2 8.8.8.8` to verify internet connectivity

### 4. Test HTTP Connectivity

```bash
docker exec <container-name> curl -v https://api.venice.ai
# or
docker exec <container-name> wget --spider https://api.venice.ai
```

**Expected**: HTTP 200 or 301/302 redirect

**If failed**:
- Check Docker network/firewall settings
- Verify proxy settings if behind corporate firewall
- Test with `docker exec <container-name> curl -v https://www.google.com` to verify general internet access

### 5. Test Venice API Endpoints

Test the `/models` endpoint:

```bash
docker exec <container-name> sh -c '
curl -X GET "https://api.venice.ai/api/v1/models" \
  -H "Authorization: Bearer $VENICE_API_KEY" \
  -H "Content-Type: application/json"
'
```

Test the `/chat/completions` endpoint:

```bash
docker exec <container-name> sh -c '
curl -X POST "https://api.venice.ai/api/v1/chat/completions" \
  -H "Authorization: Bearer $VENICE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '"'"'{"model":"qwen3-4b","messages":[{"role":"user","content":"ping"}],"max_tokens":8}'"'"'
'
```

**Expected**: JSON response with model list or chat completion

**If 404**:
- Verify URL includes `/api/v1` suffix
- Check that the endpoint path is correct (should be `/chat/completions`, not `/v1/chat/completions`)

**If 401**:
- Verify API key is correct and not expired
- Check that Authorization header format is `Bearer <key>`

## Common Issues and Solutions

### Issue: API Key Not Propagating

**Symptoms**: `VENICE_API_KEY` is missing in container

**Solutions**:
1. Check `.env` file exists and contains `VENICE_API_KEY=...`
2. Verify `docker-compose.yml` includes `.env` in `env_file:` section
3. Restart containers: `docker-compose down && docker-compose up -d`
4. Check for typos: `VENICE_API_KEY` not `VENICE_API_KEY_` or `VENICE_APIKEY`

### Issue: DNS Resolution Fails

**Symptoms**: Cannot resolve `api.venice.ai`

**Solutions**:
1. Check Docker DNS settings in `docker-compose.yml`:
   ```yaml
   dns:
     - 8.8.8.8
     - 8.8.4.4
   ```
2. Verify host machine DNS is working: `nslookup api.venice.ai`
3. Check Docker network mode (bridge vs host)

### Issue: 404 Not Found

**Symptoms**: `venice_404` error in logs

**Solutions**:
1. Verify `VENICE_API_BASE_URL` includes `/api/v1` suffix
2. Check logs for actual URL being called (enhanced logging shows this)
3. Ensure no trailing slashes: `https://api.venice.ai/api/v1` not `https://api.venice.ai/api/v1/`
4. Test with curl from inside container to isolate code vs network issue

### Issue: Connection Timeout

**Symptoms**: Timeout errors when calling Venice API

**Solutions**:
1. Check firewall/proxy settings
2. Verify container has internet access: `docker exec <container> ping -c 2 8.8.8.8`
3. Increase timeout in `libs/venice_sdk/client.py` if needed (default 30s)
4. Check if Venice API is experiencing outages

## Enhanced Logging

The codebase now includes enhanced error logging that shows:
- Full URL being called
- HTTP status codes
- Request exceptions with details
- API key presence (without exposing the key)

Check logs for entries like:
```
Venice API POST failed: https://api.venice.ai/api/v1/chat/completions -> ... (status=404)
Heartbeat attempt: model=qwen3-4b, base_url=https://api.venice.ai/api/v1, api_key_set=True
```

## Environment File Precedence

Docker Compose reads environment variables in this order (later overrides earlier):
1. `.env` (base configuration)
2. `.env.docker` (Docker-specific defaults)
3. `docker/.env.local` (local overrides, gitignored)
4. `environment:` section in `docker-compose.yml` (container-specific)

Ensure `VENICE_API_KEY` is set in at least one of these files.

## Testing After Fixes

After making changes:
1. Restart containers: `docker-compose restart orchestrator`
2. Check logs: `docker logs -f <container-name> | grep -i heartbeat`
3. Run diagnostics: `bash scripts/docker_venice_diagnose.sh`
4. Verify heartbeat succeeds in logs

