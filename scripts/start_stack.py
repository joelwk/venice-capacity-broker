from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

from apps import _path as _app_path
from libs.runtime.preflight import ensure_agentkit_installed

REPO_ROOT = _app_path.REPO_ROOT


@dataclass
class CommandSpec:
    name: str
    argv: List[str]
    env: Optional[Dict[str, str]] = None


def _truthy(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _detect_uv() -> Optional[str]:
    explicit = os.getenv("UV_BIN")
    if explicit and shutil.which(explicit):
        return explicit
    return shutil.which("uv")


def _python_argv(uv_bin: Optional[str], *parts: str) -> List[str]:
    if uv_bin:
        return [uv_bin, "run", "python", *parts]
    return [sys.executable, *parts]


def _module_argv(uv_bin: Optional[str], module: str, *parts: str) -> List[str]:
    if uv_bin:
        return [uv_bin, "run", module, *parts]
    return [sys.executable, "-m", module, *parts]


def _refresh_pool_catalog(uv_bin: Optional[str]) -> None:
    if not _truthy("MARKET_POOLS_REFRESH", True):
        print(f"[stack] skipping pool catalog refresh (MARKET_POOLS_REFRESH={os.getenv('MARKET_POOLS_REFRESH', '0')})", flush=True)
        return
    argv = _python_argv(uv_bin, "apps/cli/main.py", "market:pools:watch", "--once")
    print(f"[stack] refreshing pool catalog -> {' '.join(argv)}", flush=True)
    try:
        subprocess.check_call(argv)
    except subprocess.CalledProcessError as exc:
        print(f"[stack] pool catalog refresh failed (exit={exc.returncode}); continuing startup", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[stack] pool catalog refresh raised {exc}; continuing startup", flush=True)


def build_command_specs(uv_bin: Optional[str] | None = None) -> List[CommandSpec]:
    if uv_bin is None:
        uv_bin = _detect_uv()
    specs: List[CommandSpec] = []

    if _truthy("AUTOSTART_BROKER_API", True):
        host = os.getenv("AUTOSTART_BROKER_HOST", os.getenv("BROKER_API_HOST", "0.0.0.0"))
        port = os.getenv("AUTOSTART_BROKER_PORT", os.getenv("BROKER_API_PORT", "8000"))
        argv = _module_argv(
            uv_bin,
            "uvicorn",
            "apps.broker_api.app:app",
            "--host",
            str(host),
            "--port",
            str(port),
        )
        specs.append(CommandSpec(name="broker-api", argv=argv))

    if _truthy("AUTOSTART_ORCHESTRATOR", True):
        argv = [
            str(REPO_ROOT / "apps" / "cli" / "main.py"),
            "run:loop",
            "--sleep",
            os.getenv("AUTOSTART_ORCHESTRATOR_INTERVAL", "15"),
            "--max-cycles",
            "0",
        ]
        if _truthy("AUTOSTART_ORCHESTRATOR_LIVE", False):
            argv.append("--enable-live")
        specs.append(CommandSpec(name="agent-loop", argv=_python_argv(uv_bin, *argv)))

    if _truthy("AUTOSTART_STAKEMASTER", False):
        argv = [
            "apps/cli/main.py",
            "run:stakemaster",
        ]
        if _truthy("AUTOSTART_STAKEMASTER_LIVE", False):
            argv.append("--enable-live")
        specs.append(CommandSpec(name="stakemaster", argv=_python_argv(uv_bin, *argv)))

    if _truthy("AUTOSTART_TOKEN_WATCHER", True):
        has_key = any(os.getenv(name) for name in ("ETHERSCAN_API_KEY", "BASESCAN_API_KEY"))
        if has_key or _truthy("AUTOSTART_TOKEN_WATCHER_ALLOW_NO_KEY", False):
            argv = ["services/marketdata/token_watcher.py"]
            specs.append(CommandSpec(name="token-watcher", argv=_python_argv(uv_bin, *argv)))
        else:
            print("[stack] skipping token watcher: set ETHERSCAN_API_KEY or BASESCAN_API_KEY")

    return specs


class ProcessManager:
    def __init__(self) -> None:
        self.processes: List[tuple[str, subprocess.Popen[str]]] = []
        self._shutdown = False

    def launch(self, specs: List[CommandSpec]) -> None:
        base_env = os.environ.copy()
        for spec in specs:
            env = base_env.copy()
            if spec.env:
                env.update(spec.env)
            try:
                proc = subprocess.Popen(spec.argv, env=env)
            except FileNotFoundError as exc:
                raise RuntimeError(f"failed to launch {spec.name}: {exc}") from exc
            self.processes.append((spec.name, proc))
            print(f"[stack] started {spec.name} (pid={proc.pid}) -> {' '.join(spec.argv)}", flush=True)

    def monitor(self) -> None:
        try:
            while not self._shutdown:
                for name, proc in list(self.processes):
                    code = proc.poll()
                    if code is not None:
                        raise RuntimeError(f"process '{name}' exited with code {code}")
                time.sleep(1.0)
        except KeyboardInterrupt:
            print("[stack] received keyboard interrupt, shutting down...", flush=True)
        except RuntimeError as exc:
            print(f"[stack] {exc}", flush=True)
        finally:
            self.stop_all()

    def stop_all(self) -> None:
        if self._shutdown:
            return
        self._shutdown = True
        deadline = time.time() + float(os.getenv("AUTOSTART_SHUTDOWN_TIMEOUT", "10"))
        for name, proc in self.processes:
            if proc.poll() is None:
                print(f"[stack] terminating {name} (pid={proc.pid})", flush=True)
                try:
                    proc.terminate()
                except Exception:
                    proc.kill()
        while time.time() < deadline:
            if all(proc.poll() is not None for _, proc in self.processes):
                break
            time.sleep(0.5)
        for name, proc in self.processes:
            if proc.poll() is None:
                print(f"[stack] killing {name} (pid={proc.pid})", flush=True)
                proc.kill()


def main() -> None:
    try:
        ensure_agentkit_installed()
    except RuntimeError as exc:
        print(f"[stack] {exc}", flush=True)
        sys.exit(2)

    uv_bin = _detect_uv()
    _refresh_pool_catalog(uv_bin)
    specs = build_command_specs()
    if not specs:
        print("[stack] no processes configured. Enable AUTOSTART_* env vars to launch components.")
        return
    manager = ProcessManager()
    manager.launch(specs)
    manager.monitor()


if __name__ == "__main__":
    main()
