"""
middlewares/security.py
-----------------------
- Request logging with a request id (method, path, status, time). Query
  strings are never logged because manage links carry secret tokens.
- CSRF guard: every state-changing /api/ call must send the header
  "X-Centra: 1". Browsers won't let another site add that header to a
  request, so a malicious page can't use the owner's cookie. Razorpay's
  webhook and the cron endpoint are exempt (they carry their own secrets).
- Security headers, including a content policy that allows Razorpay
  Checkout and Google Fonts and nothing else external.
"""
from __future__ import annotations

import secrets
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app_platform.logger import get_logger

log = get_logger("http")

CSRF_EXEMPT = ("/api/payments/webhook", "/api/internal/")
UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}

CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' https://checkout.razorpay.com; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com data:; "
    "img-src 'self' data: blob: https:; "
    "connect-src 'self' https://*.razorpay.com; "
    "frame-src https://*.razorpay.com; "
    "base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
)


class SecurityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        rid = secrets.token_hex(4)
        start = time.perf_counter()
        path = request.url.path
        if (request.method in UNSAFE and path.startswith("/api/")
                and not path.startswith(CSRF_EXEMPT) and request.headers.get("x-centra") != "1"):
            log.warning("csrf header missing rid=%s path=%s", rid, path)
            return JSONResponse({"error": {"code": "csrf", "message": "Please refresh the page and try again."}},
                                status_code=403)
        response = await call_next(request)
        response.headers["X-Request-Id"] = rid
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers.setdefault("Content-Security-Policy", CSP)
        if path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        ms = (time.perf_counter() - start) * 1000
        if not path.startswith(("/static/", "/uploads/")):
            log.info("rid=%s %s %s %s %.0fms", rid, request.method, path, response.status_code, ms)
        return response
