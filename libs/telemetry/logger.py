import atexit
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

__all__ = ["get_logger"]

_DEFAULT_FMT = "%(asctime)s | %(levelname)s | %(component)s | %(message)s"
_DATE_FMT = "%Y-%m-%d %H:%M:%S"

_LOCK = Lock()
_CONSOLE_CAPTURED = False
_LOG_FILE_HANDLE = None
_LOG_PATH: Path | None = None
_RESERVED_RECORD_ATTRS = {
    "name",
    "msg",
    "args",
    "levelname",
    "levelno",
    "pathname",
    "filename",
    "module",
    "exc_info",
    "exc_text",
    "stack_info",
    "lineno",
    "funcName",
    "created",
    "msecs",
    "relativeCreated",
    "thread",
    "threadName",
    "processName",
    "process",
    "message",
    "component",
}


class _ConsoleTee:
    """Mirror writes to the original stream and an optional file sink."""

    def __init__(self, primary, mirror):
        self._primary = primary
        self._mirror = mirror
        self.encoding = getattr(primary, "encoding", "utf-8")
        self.errors = getattr(primary, "errors", "strict")

    def write(self, data):  # type: ignore[override]
        if not data:
            return 0
        try:
            self._primary.write(data)
        except Exception:
            pass
        if self._mirror is not None:
            try:
                self._mirror.write(data)
            except Exception:
                pass
        self.flush()
        return len(data)

    def writelines(self, lines):  # type: ignore[override]
        for line in lines:
            self.write(line)

    def flush(self):  # type: ignore[override]
        try:
            self._primary.flush()
        except Exception:
            pass
        if self._mirror is not None:
            try:
                self._mirror.flush()
            except Exception:
                pass

    def isatty(self):  # type: ignore[override]
        return getattr(self._primary, "isatty", lambda: False)()

    def fileno(self):  # type: ignore[override]
        return getattr(self._primary, "fileno", lambda: -1)()

    @property
    def buffer(self):  # type: ignore[override]
        return getattr(self._primary, "buffer", None)

    def __getattr__(self, item):
        return getattr(self._primary, item)


class _ComponentFormatter(logging.Formatter):
    """Inject a component label derived from the logger name."""

    def format(self, record: logging.LogRecord) -> str:  # type: ignore[override]
        if not hasattr(record, "component"):
            record.component = _component_label(record.name)
        return super().format(record)


class _JsonFormatter(logging.Formatter):
    """Emit structured log records for downstream collectors."""

    def format(self, record: logging.LogRecord) -> str:  # type: ignore[override]
        if not hasattr(record, "component"):
            record.component = _component_label(record.name)
        ts = datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat()
        payload = {
            "timestamp": ts,
            "level": record.levelname,
            "component": getattr(record, "component", record.name),
            "logger": record.name,
            "message": record.getMessage(),
            "pid": os.getpid(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        extras = {}
        for key, value in record.__dict__.items():
            if key in _RESERVED_RECORD_ATTRS or key.startswith("_"):
                continue
            try:
                json.dumps(value)
                extras[key] = value
            except TypeError:
                extras[key] = str(value)
        if extras:
            payload["extra"] = extras
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=True)


def _component_label(name: str) -> str:
    if not name:
        return "APP"
    mappings = (
        ("agent.", "AGENT"),
        ("workflow.", "WORKFLOW"),
        ("broker.", "BROKER"),
        ("services.", "SERVICE"),
        ("apps.", "APP"),
        ("graph.", "GRAPH"),
        ("libs.", "LIB"),
    )
    for prefix, label in mappings:
        if name.startswith(prefix):
            suffix = name.split(".", 1)[1] if "." in name else ""
            suffix = suffix.replace(".", ":")
            return f"{label}[{suffix or label.lower()}]"
    return name


class _RedactSecretsFilter(logging.Filter):
    """Best-effort redaction for common secret-bearing fields.

    Masks Authorization bearer tokens and environment-like key/value pairs
    for known secret keys. This is a defensive filter and should not be relied
    upon as the only control; avoid logging secrets at the source whenever possible.
    """

    _SECRET_KEYS = (
        # header/env names (lowercased)
        "authorization",
        "broker_admin_token",
        "venice_api_key",
        "venice_parent_key",
        "eth_private_key",
        "cdp_api_key_id",
        "cdp_api_key_secret",
        "cdp_wallet_secret",
        "etherscan_api_key",
        "basescan_api_key",
        "langchain_api_key",
        "kv_api_token",
        "replit_db_url",
    )

    _BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]+")

    def _scrub_text(self, text: str) -> str:
        try:
            if not isinstance(text, str):
                return text  # type: ignore[return-value]
            redacted = self._BEARER_RE.sub("Bearer <redacted>", text)
            # redact patterns like key=value or key: value
            for key in self._SECRET_KEYS:
                pattern = re.compile(rf"(?i)\b{re.escape(key)}\s*[:=]\s*[^\s,;]+")
                redacted = pattern.sub(f"{key}=<redacted>", redacted)
            return redacted
        except Exception:
            return text

    def filter(self, record: logging.LogRecord) -> bool:  # type: ignore[override]
        try:
            if isinstance(record.msg, str):
                record.msg = self._scrub_text(record.msg)
            # Common attribute-style fields
            for field_name in ("Authorization", "authorization"):
                if getattr(record, field_name, None):
                    setattr(record, field_name, "<redacted>")
            # Scrub extras if present in dict form
            for attr in ("extra",):
                val = getattr(record, attr, None)
                if isinstance(val, dict):
                    for k in list(val.keys()):
                        if str(k).lower() in self._SECRET_KEYS:
                            val[k] = "<redacted>"
                        elif isinstance(val[k], str):
                            val[k] = self._scrub_text(val[k])
        except Exception:
            # Never break logging on redaction errors
            pass
        return True


class _EnsureExtraFilter(logging.Filter):
    """Guarantee LogRecord has an `extra` attribute for downstream consumers.

    Caplog tests access `record.extra` directly; standard logging only attaches
    keys from the provided `extra` mapping as attributes. When no key named
    `extra` is supplied, attribute access raises AttributeError. This filter
    attaches an empty dict when missing so record.extra is always defined.
    """

    def filter(self, record: logging.LogRecord) -> bool:  # type: ignore[override]
        if not hasattr(record, "extra") or record.extra is None:
            record.extra = {}
        return True


def _close_log_file() -> None:
    global _LOG_FILE_HANDLE
    handle = _LOG_FILE_HANDLE
    _LOG_FILE_HANDLE = None
    if handle is None:
        return
    try:
        handle.flush()
    except Exception:
        pass
    try:
        handle.close()
    except Exception:
        pass


def _resolve_log_path() -> Path | None:
    log_file_env = os.getenv("LOG_FILE")
    if (
        log_file_env
        and log_file_env.strip()
        and log_file_env.strip().lower() != "stdout"
    ):
        return Path(log_file_env.strip()).expanduser()

    log_dir_env = os.getenv("LOG_DIR", "logs")
    log_dir = (log_dir_env or "logs").strip() or "logs"
    base_name = os.getenv("LOG_BASENAME") or "runtime.log"
    base_name = (base_name or "runtime.log").strip() or "runtime.log"
    return Path(log_dir).expanduser() / base_name


def _rotate_existing_log(target: Path) -> None:
    try:
        if not target.exists():
            return
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        suffix = target.suffix or ".log"
        rotated = target.with_name(f"{target.stem}-{timestamp}{suffix}")
        counter = 1
        while rotated.exists():
            rotated = target.with_name(f"{target.stem}-{timestamp}-{counter}{suffix}")
            counter += 1
        target.rename(rotated)
    except Exception:
        # Best effort rotation; continue if rename fails
        pass


def _write_run_header(handle, path: Path) -> None:
    try:
        ts = datetime.now(timezone.utc).isoformat()
        header = f"==== run start {ts} pid={os.getpid()} log={path.name} ====\n"
        handle.write(header)
        handle.flush()
    except Exception:
        pass


def _ensure_console_capture() -> None:
    global _CONSOLE_CAPTURED, _LOG_FILE_HANDLE, _LOG_PATH
    if _CONSOLE_CAPTURED:
        return
    with _LOCK:
        if _CONSOLE_CAPTURED:
            return
        target_path = _resolve_log_path()
        mirror = None
        # Auto-detect pytest to avoid conflicts with pytest's capture mechanism
        _is_pytest = "pytest" in sys.modules or "_pytest" in sys.modules
        capture_flag = (
            str(os.getenv("LOG_CAPTURE_CONSOLE", "1" if not _is_pytest else "0"))
            .strip()
            .lower()
        )
        capture_enabled = capture_flag not in {"0", "false", "off", "no"}
        if capture_enabled and target_path is not None:
            try:
                if target_path.parent:
                    target_path.parent.mkdir(parents=True, exist_ok=True)
                _rotate_existing_log(target_path)
                mirror = target_path.open("a", encoding="utf-8")
                _LOG_PATH = target_path
            except Exception:
                mirror = None
        if mirror is not None:
            sys.stdout = _ConsoleTee(sys.stdout, mirror)
            sys.stderr = _ConsoleTee(sys.stderr, mirror)
            _LOG_FILE_HANDLE = mirror
            atexit.register(_close_log_file)
            if target_path is not None:
                _write_run_header(mirror, target_path)
        _CONSOLE_CAPTURED = True


def get_logger(name: str = "vvv", level: str | None = None) -> logging.Logger:
    """Return a console/file logger with consistent component tagging."""
    _ensure_console_capture()
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        fmt_env = os.getenv("LOG_FORMAT")
        if not fmt_env or not fmt_env.strip():
            formatter: logging.Formatter = _ComponentFormatter(
                _DEFAULT_FMT, datefmt=_DATE_FMT
            )
        elif fmt_env.strip().lower() == "json":
            formatter = _JsonFormatter()
        else:
            formatter = _ComponentFormatter(fmt_env, datefmt=_DATE_FMT)
        handler.setFormatter(formatter)
        # Ensure record.extra is always present for tests/consumers that expect it
        handler.addFilter(_EnsureExtraFilter())
        # Attach redaction filter to avoid accidental secret leakage
        handler.addFilter(_RedactSecretsFilter())
        logger.addHandler(handler)
    level_name = (level or os.getenv("LOG_LEVEL", "INFO")).upper()
    logger.setLevel(getattr(logging, level_name, logging.INFO))
    logger.propagate = True
    return logger
