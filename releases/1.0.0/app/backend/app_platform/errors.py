"""
app_platform/errors.py
----------------------
Every expected failure is raised as AppError with a status code and a
message written for the person using the page. The HTTP layer turns it
into {"error": {"code", "message", ...}} so the frontend can show it
as-is.
"""
from __future__ import annotations


class AppError(Exception):
    def __init__(self, status: int, code: str, message: str, **extra):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra


def bad_request(message: str, code: str = "bad_request", **extra) -> AppError:
    return AppError(400, code, message, **extra)


def unauthorized(message: str = "Please sign in to continue.") -> AppError:
    return AppError(401, "unauthorized", message)


def forbidden(message: str = "You don't have access to this.") -> AppError:
    return AppError(403, "forbidden", message)


def not_found(message: str = "Not found.") -> AppError:
    return AppError(404, "not_found", message)


def conflict(message: str, code: str = "conflict", **extra) -> AppError:
    return AppError(409, code, message, **extra)


def too_many(message: str = "Too many requests. Please wait a moment and try again.", **extra) -> AppError:
    return AppError(429, "rate_limited", message, **extra)
