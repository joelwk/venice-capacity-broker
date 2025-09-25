import atexit
import logging
import os
import sys
from pathlib import Path
from threading import Lock
from typing import Optional

__all__ = ["get_logger"]

_DEFAULT_FMT = "%(asctime)s | %(levelname)s | %(component)s | %(message)s"
_DATE_FMT = "%Y-%m-%d %H:%M:%S"

_LOCK = Lock()
_CONSOLE_CAPTURED = False
_LOG_FILE_HANDLE = None


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


def _ensure_console_capture() -> None:
    global _CONSOLE_CAPTURED, _LOG_FILE_HANDLE
    if _CONSOLE_CAPTURED:
        return
    with _LOCK:
        if _CONSOLE_CAPTURED:
            return
        log_path = os.getenv("LOG_FILE")
        if not log_path:
            log_dir = os.getenv("LOG_DIR", "logs").strip()
            if not log_dir:
                log_dir = "logs"
            log_path = str(Path(log_dir) / "runtime.log")
        mirror = None
        capture_flag = str(os.getenv("LOG_CAPTURE_CONSOLE", "1")).strip().lower()
        capture_enabled = capture_flag not in {"0", "false", "off", "no"}
        if capture_enabled and log_path.lower() != "stdout":
            try:
                path_obj = Path(log_path).expanduser()
                if path_obj.parent:
                    path_obj.parent.mkdir(parents=True, exist_ok=True)
                mirror = path_obj.open("a", encoding="utf-8")
            except Exception:
                mirror = None
        if mirror is not None:
            sys.stdout = _ConsoleTee(sys.stdout, mirror)
            sys.stderr = _ConsoleTee(sys.stderr, mirror)
            _LOG_FILE_HANDLE = mirror
            atexit.register(_close_log_file)
        _CONSOLE_CAPTURED = True


def get_logger(name: str = "vvv", level: Optional[str] = None) -> logging.Logger:
    """Return a console/file logger with consistent component tagging."""
    _ensure_console_capture()
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        fmt = os.getenv("LOG_FORMAT", _DEFAULT_FMT)
        handler.setFormatter(_ComponentFormatter(fmt, datefmt=_DATE_FMT))
        logger.addHandler(handler)
    level_name = (level or os.getenv("LOG_LEVEL", "INFO")).upper()
    logger.setLevel(getattr(logging, level_name, logging.INFO))
    logger.propagate = False
    return logger
