import logging
import os
import sys
from typing import Optional


def get_logger(name: str = "vvv", level: Optional[str] = None) -> logging.Logger:
    """Return a configured logger writing to stdout.

    Level can be overridden via arg or LOG_LEVEL env.
    """
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        fmt = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
        handler.setFormatter(logging.Formatter(fmt))
        logger.addHandler(handler)
    level_name = level or os.getenv("LOG_LEVEL", "INFO")
    logger.setLevel(getattr(logging, level_name.upper(), logging.INFO))
    return logger

