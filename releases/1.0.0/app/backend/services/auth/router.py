"""
services/auth/router.py
-----------------------
POST /api/auth/signin           email + password -> session cookie
POST /api/auth/signout
GET  /api/auth/me
POST /api/auth/forgot/request   email -> OTP email (says if no account exists, as decided)
POST /api/auth/forgot/reset     email + OTP + new password
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app_platform.config import get_settings
from app_platform.errors import AppError, not_found
from app_platform.logger import get_logger, mask_email
from app_platform.timeutil import fmt_local
from dbhandlers.database import get_db, utcnow
from dbhandlers.models import User
from middlewares.rate_limit import limit_ip
from services.auth import lockout
from services.auth.email_sender import otp_email, password_changed_email, send_email
from services.auth.otp import issue_otp, verify_otp
from services.auth.passwords import (
    DUMMY_HASH, hash_password, normalize_email, validate_email, validate_password, verify_password,
)
from services.auth.sessions import create_session, current_user, end_all_sessions, end_session, optional_user
from services.business.access import business_for_user

router = APIRouter(prefix="/api/auth", tags=["auth"])
log = get_logger("auth")

GENERIC_FAIL = "Email or password is incorrect."


class SignInIn(BaseModel):
    email: str
    password: str


class EmailIn(BaseModel):
    email: str


class ResetIn(BaseModel):
    email: str
    code: str
    password: str
    confirm: str


def _locked_error(user: User) -> AppError:
    s = get_settings()
    return AppError(
        423, "account_locked",
        f"Too many incorrect attempts. Your account is locked until {fmt_local(user.locked_until)}. "
        f"Try again after that, or contact {s.support_email}.",
        locked_until=user.locked_until.isoformat() + "Z", contact=s.support_email,
    )


def _landing_for(db: Session, user: User) -> str:
    biz = business_for_user(db, user)
    return "/dashboard" if biz and biz.status != "draft" else "/signup"


@router.post("/signin")
def signin(body: SignInIn, request: Request, response: Response, db: Session = Depends(get_db)):
    limit_ip(request, "signin", 30, 300)
    email = normalize_email(body.email)
    user = db.scalars(select(User).where(User.email == email)).first()
    now = utcnow()
    if user is None:
        verify_password(body.password, DUMMY_HASH)  # same timing as a real check
        raise AppError(401, "bad_credentials", GENERIC_FAIL)
    if lockout.is_locked(user, now):
        raise _locked_error(user)
    if not verify_password(body.password, user.password_hash):
        outcome = lockout.register_failure(user, now)
        db.commit()
        log.info("signin failed user=%s locked=%s", user.id, outcome.locked)
        if outcome.locked:
            raise _locked_error(user)
        extra = {"attempts_left": outcome.attempts_left} if outcome.show_warning else {}
        msg = GENERIC_FAIL
        if outcome.show_warning:
            word = "attempt" if outcome.attempts_left == 1 else "attempts"
            msg = f"{GENERIC_FAIL} {outcome.attempts_left} {word} left."
        raise AppError(401, "bad_credentials", msg, **extra)
    lockout.register_success(user)
    create_session(db, response, user)
    db.commit()
    log.info("signin ok user=%s", user.id)
    return {"ok": True, "redirect": _landing_for(db, user)}


@router.post("/signout")
def signout(request: Request, response: Response, db: Session = Depends(get_db)):
    end_session(db, request, response)
    db.commit()
    return {"ok": True}


@router.get("/me")
def me(db: Session = Depends(get_db), user: User = Depends(current_user)):
    biz = business_for_user(db, user)
    return {
        "user": {"id": user.id, "name": user.name, "email": user.email, "phone": user.phone},
        "business": None if biz is None else {"id": biz.id, "name": biz.name, "status": biz.status, "slug": biz.slug},
        "redirect": _landing_for(db, user),
    }


@router.get("/session")
def session_state(db: Session = Depends(get_db), user: User | None = Depends(optional_user)):
    """Lets the sign-in page skip straight to the dashboard if already signed in."""
    if user is None:
        return {"signed_in": False}
    return {"signed_in": True, "redirect": _landing_for(db, user)}


@router.post("/forgot/request")
def forgot_request(body: EmailIn, request: Request, db: Session = Depends(get_db)):
    limit_ip(request, "forgot", 10, 600)
    email = validate_email(body.email)
    user = db.scalars(select(User).where(User.email == email)).first()
    if user is None:
        # Decided by RR: tell the person the account doesn't exist.
        raise not_found("No account exists with this email. Check the spelling or sign up.")
    code = issue_otp(db, "reset", email)
    subject, text = otp_email(code, "reset")
    send_email(email, subject, text)
    db.commit()
    log.info("reset code sent to=%s", mask_email(email))
    return {"ok": True}


@router.post("/forgot/reset")
def forgot_reset(body: ResetIn, request: Request, db: Session = Depends(get_db)):
    limit_ip(request, "forgot-reset", 20, 600)
    email = validate_email(body.email)
    user = db.scalars(select(User).where(User.email == email)).first()
    if user is None:
        raise not_found("No account exists with this email.")
    validate_password(body.password, body.confirm)
    verify_otp(db, "reset", email, body.code)
    user.password_hash = hash_password(body.password)
    end_all_sessions(db, user.id)
    # Decided: a reset does NOT clear the lock or the attempt count.
    db.commit()
    subject, text = password_changed_email()
    send_email(email, subject, text, critical=False)
    still_locked = lockout.is_locked(user, utcnow())
    msg = "Your password was changed. Sign in with your new password."
    if still_locked:
        msg = (f"Your password was changed. Your account is still locked until "
               f"{fmt_local(user.locked_until)}; sign in with the new password after that.")
    return {"ok": True, "message": msg, "still_locked": still_locked}
