from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass

from apps import _path as _app_path
from libs.runtime.preflight import ensure_agentkit_installed

REPO_ROOT = _app_path.REPO_ROOT


@dataclass
class CommandSpec:
    name: str
    argv: list[str]
    env: dict[str, str] | None = None


def _truthy(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _is_port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    """Check if a TCP port is open and accepting connections."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        result = sock.connect_ex((host, port))
        sock.close()
        return result == 0
    except Exception:
        return False


def _wait_for_port(
    host: str,
    port: int,
    max_wait: float = 30.0,
    check_interval: float = 0.5,
    process: subprocess.Popen[str] | None = None,
) -> bool:
    """
    Wait for a port to become open, returning True if it opens within max_wait seconds.

    Stops waiting early if the tracked process exits to avoid hanging on failed boots.
    """
    deadline = time.time() + max_wait
    while time.time() < deadline:
        if _is_port_open(host, port, timeout=check_interval):
            return True
        if process is not None and process.poll() is not None:
            return False
        time.sleep(check_interval)
    return False


def _detect_uv() -> str | None:
    explicit = os.getenv("UV_BIN")
    if explicit and shutil.which(explicit):
        return explicit
    return shutil.which("uv")


def _python_argv(uv_bin: str | None, *parts: str) -> list[str]:
    if uv_bin:
        return [uv_bin, "run", "python", *parts]
    return [sys.executable, *parts]


def _module_argv(uv_bin: str | None, module: str, *parts: str) -> list[str]:
    if uv_bin:
        return [uv_bin, "run", module, *parts]
    return [sys.executable, "-m", module, *parts]


def _refresh_pool_catalog(uv_bin: str | None) -> None:
    if not _truthy("MARKET_POOLS_REFRESH", True):
        print(
            f"[stack] skipping pool catalog refresh (MARKET_POOLS_REFRESH={os.getenv('MARKET_POOLS_REFRESH', '0')})",
            flush=True,
        )
        return
    argv = _python_argv(uv_bin, "apps/cli/main.py", "market:pools:watch", "--once")
    print(f"[stack] refreshing pool catalog -> {' '.join(argv)}", flush=True)
    try:
        subprocess.check_call(argv)
    except subprocess.CalledProcessError as exc:
        print(
            f"[stack] pool catalog refresh failed (exit={exc.returncode}); continuing startup",
            flush=True,
        )
    except Exception as exc:
        print(
            f"[stack] pool catalog refresh raised {exc}; continuing startup", flush=True
        )


def _refresh_pool_catalog_async(uv_bin: str | None) -> None:
    if not _truthy("MARKET_POOLS_REFRESH", True):
        return

    def runner() -> None:
        print("[stack] refreshing pool catalog in background...", flush=True)
        try:
            _refresh_pool_catalog(uv_bin)
        except Exception as exc:
            print(
                f"[stack] async pool catalog refresh raised {exc}; continuing startup",
                flush=True,
            )

    thread = threading.Thread(target=runner, name="pool-refresh", daemon=True)
    thread.start()


def build_command_specs(uv_bin: str | None = None) -> list[CommandSpec]:
    if uv_bin is None:
        uv_bin = _detect_uv()
    specs: list[CommandSpec] = []

    if _truthy("AUTOSTART_BROKER_API", True):
        host = os.getenv(
            "AUTOSTART_BROKER_HOST", os.getenv("BROKER_API_HOST", "0.0.0.0")
        )
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
        # Progressive-live: start dry, escalate to live after healthy heartbeats
        if _truthy("STAKEMASTER_PROGRESSIVE_ENABLE", False):
            argv.append("--progressive-live")
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
        has_key = any(
            os.getenv(name) for name in ("ETHERSCAN_API_KEY", "BASESCAN_API_KEY")
        )
        if has_key or _truthy("AUTOSTART_TOKEN_WATCHER_ALLOW_NO_KEY", False):
            argv = ["services/marketdata/token_watcher.py"]
            specs.append(
                CommandSpec(name="token-watcher", argv=_python_argv(uv_bin, *argv))
            )
        else:
            print(
                "[stack] skipping token watcher: set ETHERSCAN_API_KEY or BASESCAN_API_KEY"
            )

    return specs


LOGGING_ENV_VARS = (
    "LOG_LEVEL",
    "LOG_FORMAT",
    "LOG_DIR",
    "LOG_BASENAME",
    "LOG_CAPTURE_CONSOLE",
    "LOG_FILE",
)


class ProcessManager:
    def __init__(self) -> None:
        self.processes: list[tuple[str, subprocess.Popen[str]]] = []
        self._shutdown = False

    def launch(self, specs: list[CommandSpec]) -> None:
        base_env = os.environ.copy()
        # Ensure logging environment variables are explicitly propagated
        # with sensible defaults for subprocess consistency
        logging_defaults = {
            "LOG_LEVEL": "INFO",
            "LOG_DIR": "logs",
            "LOG_BASENAME": "runtime.log",
            "LOG_CAPTURE_CONSOLE": "1",
        }
        for key, default in logging_defaults.items():
            if key not in base_env:
                base_env[key] = default
        broker_port = None
        broker_host = None

        for spec in specs:
            env = base_env.copy()
            if spec.env:
                env.update(spec.env)
            try:
                proc = subprocess.Popen(spec.argv, env=env)
            except FileNotFoundError as exc:
                raise RuntimeError(f"failed to launch {spec.name}: {exc}") from exc
            self.processes.append((spec.name, proc))
            print(
                f"[stack] started {spec.name} (pid={proc.pid}) -> {' '.join(spec.argv)}",
                flush=True,
            )

            # Wait for broker API to bind to port before starting other processes
            if spec.name == "broker-api":
                broker_host = os.getenv(
                    "AUTOSTART_BROKER_HOST", os.getenv("BROKER_API_HOST", "0.0.0.0")
                )
                broker_port_str = os.getenv(
                    "AUTOSTART_BROKER_PORT", os.getenv("BROKER_API_PORT", "8000")
                )
                try:
                    broker_port = int(broker_port_str)
                except ValueError:
                    broker_port = 8000
                wait_host = broker_host
                if wait_host in {"0.0.0.0", "", None}:
                    wait_host = "127.0.0.1"
                elif wait_host == "::":
                    wait_host = "::1"

                print(
                    f"[stack] waiting for broker API to bind to {broker_host}:{broker_port} (probe {wait_host})...",
                    flush=True,
                )
                raw_timeout = os.getenv("AUTOSTART_BROKER_WAIT_SECONDS") or os.getenv(
                    "BROKER_API_WAIT_SECONDS"
                )
                try:
                    wait_seconds = (
                        float(raw_timeout) if raw_timeout is not None else 90.0
                    )
                except ValueError:
                    wait_seconds = 90.0

                if _wait_for_port(
                    wait_host, broker_port, max_wait=wait_seconds, process=proc
                ):
                    print(
                        f"[stack] broker API ready on {broker_host}:{broker_port}",
                        flush=True,
                    )
                else:
                    status = proc.poll()
                    if status is not None:
                        print(
                            f"[stack] broker API process exited with code {status} before port {broker_host}:{broker_port} opened",
                            flush=True,
                        )
                    else:
                        print(
                            f"[stack] warning: broker API {broker_host}:{broker_port} not ready after {wait_seconds:.0f}s, continuing anyway",
                            flush=True,
                        )

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


def _log_logging_config() -> None:
    """Print active logging configuration for diagnostics."""
    log_level = os.getenv("LOG_LEVEL", "INFO")
    log_format = os.getenv("LOG_FORMAT", "(default)")
    log_dir = os.getenv("LOG_DIR", "logs")
    log_basename = os.getenv("LOG_BASENAME", "runtime.log")
    log_capture = os.getenv("LOG_CAPTURE_CONSOLE", "1")
    log_file = os.getenv("LOG_FILE", "")

    print(
        f"[stack] logging config: level={log_level} format={log_format or 'text'} "
        f"capture_console={log_capture}",
        flush=True,
    )
    if log_file:
        print(f"[stack] logging to: {log_file}", flush=True)
    else:
        print(f"[stack] logging to: {log_dir}/{log_basename}", flush=True)


def main() -> None:
    try:
        ensure_agentkit_installed()
    except RuntimeError as exc:
        print(f"[stack] {exc}", flush=True)
        sys.exit(2)

    _log_logging_config()
    uv_bin = _detect_uv()
    _refresh_pool_catalog_async(uv_bin)
    specs = build_command_specs()
    if not specs:
        print(
            "[stack] no processes configured. Enable AUTOSTART_* env vars to launch components."
        )
        return
    manager = ProcessManager()
    manager.launch(specs)
    manager.monitor()


if __name__ == "__main__":
    main()
