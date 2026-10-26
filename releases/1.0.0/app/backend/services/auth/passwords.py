"""
services/auth/passwords.py
--------------------------
Password rules and hashing (scrypt from the standard library), plus the
helpers used for every secret token in the app.

Decided rule: at least 8 characters, with letters, numbers and at least
one special character.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets

from app_platform.config import get_settings
from app_platform.errors import bad_request

_POLICY = re.compile(r"^(?=.*[A-Za-z])(?=.*\d)(?=.*[^A-Za-z\d]).{8,128}$")
POLICY_MESSAGE = "Password must be at least 8 characters and include letters, numbers and a special character."

_N, _R, _P = 2**14, 8, 1


def validate_password(password: str, confirm: str | None = None) -> None:
    if not _POLICY.match(password or ""):
        raise bad_request(POLICY_MESSAGE, code="weak_password")
    if confirm is not None and password != confirm:
        raise bad_request("Your passwords do not match.", code="password_mismatch")


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return "scrypt$%d$%d$%d$%s$%s" % (
        _N, _R, _P, base64.b64encode(salt).decode(), base64.b64encode(digest).decode(),
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        _, n, r, p, salt_b64, digest_b64 = stored.split("$")
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        actual = hashlib.scrypt(password.encode(), salt=salt, n=int(n), r=int(r), p=int(p), dklen=len(expected))
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


# A real-looking hash to compare against when the email doesn't exist, so a
# wrong email and a wrong password take the same time to answer.
DUMMY_HASH = hash_password("dummy-Password-1!")


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    """Keyed hash used to store session tokens, OTP codes and booking links."""
    key = get_settings().secret_key.encode()
    return hmac.new(key, token.encode(), hashlib.sha256).hexdigest()


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


_EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def validate_email(email: str) -> str:
    email = normalize_email(email)
    if not _EMAIL.match(email) or len(email) > 254:
        raise bad_request("Enter a valid email address.", code="invalid_email")
    return email
