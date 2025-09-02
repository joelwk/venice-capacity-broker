import importlib.util
import json
from pathlib import Path
import sys

repo_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repo_root))
app_py = repo_root / 'apps' / 'broker-api' / 'app.py'
spec = importlib.util.spec_from_file_location('broker_app', str(app_py))
mod = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(mod)  # type: ignore[attr-defined]
app = getattr(mod, 'app', None)
routes = getattr(app, 'routes', []) if app is not None else []
has_admin = any(getattr(r, 'path', None) == '/admin' and r.__class__.__name__ == 'Mount' for r in routes)
print(json.dumps({'ok': app is not None, 'has_admin': has_admin, 'routes_count': len(routes)}))
