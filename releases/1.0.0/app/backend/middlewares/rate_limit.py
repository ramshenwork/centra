"""
middlewares/rate_limit.py
-------------------------
Simple sliding-window limiter kept in process memory.

Known limit: counts reset when the process restarts and are not shared
between several running copies. That is fine for one Render instance; if
the app is ever scaled to several instances, move this to Redis.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

from fastapi import Request

from app_platform.errors import too_many

_lock = threading.Lock()
_hits: dict[str, deque] = defaultdict(deque)


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def check(key: str, limit: int, per_seconds: int) -> None:
    now = time.monotonic()
    with _lock:
        q = _hits[key]
        while q and q[0] <= now - per_seconds:
            q.popleft()
        if len(q) >= limit:
            raise too_many()
        q.append(now)


def limit_ip(request: Request, bucket: str, limit: int, per_seconds: int) -> None:
    check(f"{bucket}:{client_ip(request)}", limit, per_seconds)


def reset_all() -> None:
    """Tests only."""
    with _lock:
        _hits.clear()
