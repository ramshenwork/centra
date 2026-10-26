"""
app_platform/logger.py
----------------------
Logs go to stdout (Render shows these) and to shared/logs/app/app.log.

Rule for everyone writing log lines: never log a password, OTP code,
payment signature, session token or a full phone number. Use mask_phone()
and mask_email(), and always include the business_id / booking id so a
problem can be traced to one customer.
"""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from app_platform.config import LOG_DIR

_CONFIGURED = False


def setup_logging(level: int = logging.INFO) -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    root = logging.getLogger("centra")
    root.setLevel(level)
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    root.addHandler(stream)
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(LOG_DIR / "app.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
    except OSError:
        root.warning("could not open shared/logs/app/app.log; logging to stdout only")
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"centra.{name}")


def mask_phone(phone: str | None) -> str:
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    return f"***{digits[-3:]}" if len(digits) >= 3 else "***"


def mask_email(email: str | None) -> str:
    email = email or ""
    if "@" not in email:
        return "***"
    name, _, domain = email.partition("@")
    return f"{name[:1]}***@{domain}"
