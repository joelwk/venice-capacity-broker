#!/bin/bash
# Quick diagnostic script to test Venice API connectivity from Docker containers

set -e

CONTAINER_NAME="${1:-venice-orchestrator-1}"

echo "=========================================="
echo "Venice API Docker Diagnostics"
echo "Container: $CONTAINER_NAME"
echo "=========================================="
echo ""

# Check if container is running
if ! docker ps --format "{{.Names}}" | grep -q "^${CONTAINER_NAME}$"; then
    echo "Error: Container '$CONTAINER_NAME' is not running"
    echo ""
    echo "Available containers:"
    docker ps --format "  {{.Names}}"
    exit 1
fi

echo "1. Checking environment variables..."
echo "-----------------------------------"
docker exec "$CONTAINER_NAME" sh -c '
    echo "VENICE_API_KEY: $([ -n "$VENICE_API_KEY" ] && echo "SET (${#VENICE_API_KEY} chars)" || echo "MISSING")"
    echo "VENICE_API_BASE_URL: ${VENICE_API_BASE_URL:-NOT SET}"
    echo "VENICE_HEARTBEAT_MODEL: ${VENICE_HEARTBEAT_MODEL:-NOT SET}"
'
echo ""

echo "2. Testing DNS resolution..."
echo "-----------------------------------"
docker exec "$CONTAINER_NAME" sh -c '
    if nslookup api.venice.ai > /dev/null 2>&1 || getent hosts api.venice.ai > /dev/null 2>&1; then
        echo "✓ api.venice.ai resolves"
    else
        echo "✗ api.venice.ai DNS resolution failed"
    fi
'
echo ""

echo "3. Testing HTTP connectivity (using Python requests)..."
echo "-----------------------------------"
docker exec "$CONTAINER_NAME" python -c "
import sys
import requests
from urllib.parse import urlparse

def test_url(url, timeout=10):
    try:
        resp = requests.get(url, timeout=timeout, allow_redirects=True)
        return True, resp.status_code, None
    except requests.exceptions.Timeout:
        return False, None, 'Timeout'
    except requests.exceptions.SSLError as e:
        return False, None, f'SSL Error: {str(e)[:100]}'
    except requests.exceptions.ConnectionError as e:
        return False, None, f'Connection Error: {str(e)[:100]}'
    except Exception as e:
        return False, None, f'Error: {str(e)[:100]}'

print('Testing HTTPS connection to api.venice.ai...')
ok, code, err = test_url('https://api.venice.ai')
if ok:
    print(f'✓ Can reach https://api.venice.ai (HTTP {code})')
else:
    print(f'✗ Cannot reach https://api.venice.ai')
    print(f'  Error: {err}')
    print('')
    print('Testing basic internet connectivity...')
    ok2, code2, err2 = test_url('https://www.google.com', timeout=5)
    if ok2:
        print('  ✓ Can reach external HTTPS sites (internet works)')
    else:
        print(f'  ✗ Cannot reach external HTTPS sites: {err2}')
"
echo ""

echo "4. Testing Python availability and script path..."
echo "-----------------------------------"
docker exec "$CONTAINER_NAME" sh -c '
    echo "Python version: $(python --version 2>&1)"
    echo "Working directory: $(pwd)"
    echo "Script exists check:"
    if [ -f scripts/diagnose_docker_venice.py ]; then
        echo "  ✓ scripts/diagnose_docker_venice.py found"
    elif [ -f /app/scripts/diagnose_docker_venice.py ]; then
        echo "  ✓ /app/scripts/diagnose_docker_venice.py found"
        cd /app
    else
        echo "  ✗ Script not found in current dir or /app"
        echo "  Searching for script..."
        find . -name "diagnose_docker_venice.py" 2>/dev/null | head -3 || echo "  Not found"
    fi
'
echo ""

echo "5. Running Python diagnostics..."
echo "-----------------------------------"
docker exec "$CONTAINER_NAME" python -c "
import sys
import os
sys.path.insert(0, '/app')

# Try to import and run the diagnostic script
try:
    # Check if script exists
    script_path = '/app/scripts/diagnose_docker_venice.py'
    if os.path.exists(script_path):
        print(f'Found script at: {script_path}')
        print('Running diagnostic...')
        print('=' * 80)
        exec(open(script_path).read())
    else:
        print('Script not found at /app/scripts/diagnose_docker_venice.py')
        print('Running inline diagnostics instead...')
        print('=' * 80)
        
        # Inline version of key diagnostics
        import requests
        
        base_url = os.getenv('VENICE_API_BASE_URL', 'https://api.venice.ai/api/v1')
        api_key = os.getenv('VENICE_API_KEY')
        model = os.getenv('VENICE_HEARTBEAT_MODEL', 'qwen3-4b')
        
        print(f'Base URL: {base_url}')
        print(f'API Key: {\"SET\" if api_key else \"MISSING\"}')
        print(f'Model: {model}')
        print()
        
        # Test /models endpoint
        print('Testing /models endpoint...')
        try:
            headers = {'Authorization': f'Bearer {api_key}'} if api_key else {}
            resp = requests.get(f'{base_url}/models', headers=headers, timeout=10)
            print(f'  Status: {resp.status_code}')
            if resp.status_code == 200:
                data = resp.json()
                models = data.get('data', [])
                print(f'  ✓ Success: {len(models)} models available')
            else:
                print(f'  ✗ Failed: {resp.text[:200]}')
        except Exception as e:
            print(f'  ✗ Error: {e}')
        
        print()
        
        # Test /chat/completions endpoint
        print(f'Testing /chat/completions endpoint (model={model})...')
        try:
            headers = {'Authorization': f'Bearer {api_key}'} if api_key else {}
            headers['Content-Type'] = 'application/json'
            payload = {
                'model': model,
                'messages': [{'role': 'user', 'content': 'ping'}],
                'max_tokens': 8
            }
            resp = requests.post(f'{base_url}/chat/completions', json=payload, headers=headers, timeout=30)
            print(f'  Status: {resp.status_code}')
            if resp.status_code == 200:
                print('  ✓ Success: Chat completions endpoint works')
            else:
                print(f'  ✗ Failed: {resp.text[:200]}')
        except Exception as e:
            print(f'  ✗ Error: {e}')
            
except Exception as e:
    print(f'Error running diagnostics: {e}')
    import traceback
    traceback.print_exc()
"

