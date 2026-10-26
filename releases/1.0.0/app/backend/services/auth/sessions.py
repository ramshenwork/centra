"""
services/auth/sessions.py
-------------------------
Signed-in state is an HttpOnly cookie holding a random token. Only a
keyed hash of the token is stored, so a database leak doesn't hand out
live sessions. Sessions last SESSION_HOURS (default 12).
"""
from __future__ import annotations

from datetime import timedelta

from fastapi import Depends, Request, Response
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app_platform.config import get_settings
from app_platform.errors import unauthorized
from dbhandlers.database import get_db, utcnow
from dbhandlers.models import SessionToken, User
from services.auth.passwords import new_token, token_hash

COOKIE_NAME = "centra_session"


def create_session(db: Session, response: Response, user: User) -> None:
    s = get_settings()
    token = new_token()
    db.add(SessionToken(user_id=user.id, token_hash=token_hash(token),
                        expires_at=utcnow() + timedelta(hours=s.session_hours)))
    db.flush()
    response.set_cookie(
        COOKIE_NAME, token, max_age=s.session_hours * 3600, httponly=True,
        secure=s.cookie_secure, samesite="lax", path="/",
    )


def end_session(db: Session, request: Request, response: Response) -> None:
    token = request.cookies.get(COOKIE_NAME)
    if token:
        db.execute(delete(SessionToken).where(SessionToken.token_hash == token_hash(token)))
    response.delete_cookie(COOKIE_NAME, path="/")


def end_all_sessions(db: Session, user_id: int) -> None:
    db.execute(delete(SessionToken).where(SessionToken.user_id == user_id))


def optional_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    row = db.scalars(select(SessionToken).where(SessionToken.token_hash == token_hash(token))).first()
    if row is None or row.expires_at <= utcnow():
        return None
    return db.get(User, row.user_id)


def current_user(user: User | None = Depends(optional_user)) -> User:
    if user is None:
        raise unauthorized()
    return user
