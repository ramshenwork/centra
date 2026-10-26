"""
services/business/access.py
---------------------------
Tenant isolation lives here. The business a request acts on is ALWAYS
taken from the signed-in user's membership, never from an id sent by the
browser. Every dashboard query then filters on that business_id, and
owned() refuses any row whose business_id doesn't match.
"""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app_platform.errors import AppError, not_found
from dbhandlers.database import get_db
from dbhandlers.models import Business, Membership, User
from services.auth.sessions import current_user


def business_for_user(db: Session, user: User) -> Business | None:
    m = db.scalars(select(Membership).where(Membership.user_id == user.id).order_by(Membership.id)).first()
    return db.get(Business, m.business_id) if m else None


@dataclass
class Ctx:
    db: Session
    user: User
    business: Business


def owner_ctx(db: Session = Depends(get_db), user: User = Depends(current_user)) -> Ctx:
    """Dashboard access: signed in, owns a business, and the business has finished sign-up."""
    biz = business_for_user(db, user)
    if biz is None or biz.status == "draft":
        raise AppError(409, "signup_incomplete", "Finish setting up your business first.", redirect="/signup")
    return Ctx(db, user, biz)


def draft_ctx(db: Session = Depends(get_db), user: User = Depends(current_user)) -> Ctx:
    """Sign-up wizard access: signed in and owns a business (draft or not)."""
    biz = business_for_user(db, user)
    if biz is None:
        raise not_found("No business found for this account.")
    return Ctx(db, user, biz)


def owned(db: Session, model, row_id: int, business_id: int):
    """Fetch a row only if it belongs to this business; otherwise 'not found'."""
    row = db.get(model, row_id)
    if row is None or getattr(row, "business_id", None) != business_id:
        raise not_found()
    return row
