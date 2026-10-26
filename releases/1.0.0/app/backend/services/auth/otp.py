"""
services/auth/otp.py
--------------------
Email one-time codes, shared by sign-up, forgot password and "Manage my
booking" recovery. Phone OTP is parked; when it is added it becomes a
second delivery channel here, not a copy of this file.

TESTING MODE: when TEST_OTP_CODE is set (e.g. 454545), every code issued
is that fixed code, for every email and every purpose. Anyone who knows it
can sign up as any email, reset any password and open any booking, so it
is for testing only. Delete TEST_OTP_CODE in Render to go back to real
random codes; no code change needed.

Rules:
- 6 digits, stored only as a keyed hash.
- Expires after 10 minutes and works once.
- 5 wrong tries kills the code (request a new one).
- 60 second cooldown between sends, and at most 5 sends per hour per
  email address and purpose.
- Sending a new code cancels the previous one.
"""
from __future__ import annotations

import hmac
import secrets
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app_platform.config import get_settings
from app_platform.errors import AppError, bad_request, too_many
from dbhandlers.database import utcnow
from dbhandlers.models import OtpCode
from services.auth.passwords import token_hash

OTP_TTL = timedelta(minutes=10)
RESEND_COOLDOWN = timedelta(seconds=60)
MAX_SENDS_PER_HOUR = 5
MAX_WRONG = 5


def issue_otp(db: Session, purpose: str, target: str, ref: str = "") -> str:
    now = utcnow()
    recent = db.scalars(
        select(OtpCode)
        .where(OtpCode.purpose == purpose, OtpCode.target == target, OtpCode.ref == ref,
               OtpCode.created_at > now - timedelta(hours=1))
        .order_by(OtpCode.created_at.desc())
    ).all()
    if recent and recent[0].created_at > now - RESEND_COOLDOWN:
        wait = int((recent[0].created_at + RESEND_COOLDOWN - now).total_seconds()) + 1
        raise too_many(f"Please wait {wait} seconds before requesting another code.", retry_after=wait)
    if len(recent) >= MAX_SENDS_PER_HOUR:
        raise too_many("Too many codes requested for this email. Try again in an hour.")
    for old in recent:
        if old.consumed_at is None:
            old.consumed_at = now
    code = get_settings().test_otp_code or f"{secrets.randbelow(1_000_000):06d}"
    db.add(OtpCode(purpose=purpose, target=target, ref=ref, code_hash=token_hash(code),
                   expires_at=now + OTP_TTL))
    db.flush()
    return code


def verify_otp(db: Session, purpose: str, target: str, code: str, ref: str = "") -> None:
    """Raises AppError on any failure. Commits wrong-attempt counts itself."""
    now = utcnow()
    otp = db.scalars(
        select(OtpCode)
        .where(OtpCode.purpose == purpose, OtpCode.target == target, OtpCode.ref == ref,
               OtpCode.consumed_at.is_(None))
        .order_by(OtpCode.created_at.desc())
    ).first()
    if otp is None or otp.expires_at <= now:
        raise bad_request("This code has expired. Request a new one.", code="otp_expired")
    if otp.attempts >= MAX_WRONG:
        raise bad_request("Too many wrong codes. Request a new one.", code="otp_locked")
    code = (code or "").strip()
    if not (len(code) == 6 and code.isdigit() and hmac.compare_digest(otp.code_hash, token_hash(code))):
        otp.attempts += 1
        left = MAX_WRONG - otp.attempts
        db.commit()
        if left <= 0:
            raise bad_request("Too many wrong codes. Request a new one.", code="otp_locked")
        raise AppError(400, "otp_wrong", "That code doesn't match. Check the email and try again.", attempts_left=left)
    otp.consumed_at = now
    db.flush()