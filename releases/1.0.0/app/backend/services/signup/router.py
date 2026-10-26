"""
services/signup/router.py
-------------------------
GET  /api/signup/catalog          preloaded business types and services
GET  /api/signup/pricing          plan prices (from shared/config/pricing.json)
GET  /api/signup/slug-available
POST /api/signup/start            owner details -> email OTP
POST /api/signup/resend
POST /api/signup/verify           OTP -> account + draft business, signed in
GET  /api/signup/draft            resume the wizard
PUT  /api/signup/draft            save wizard progress
POST /api/signup/logo             PNG upload
POST /api/signup/checkout         validate + save everything, create Razorpay order
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, Request, Response, UploadFile
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app_platform.errors import AppError, bad_request, conflict, not_found
from app_platform.logger import get_logger, mask_email
from dbhandlers.database import get_db, utcnow
from dbhandlers.models import Business, Membership, PendingSignup, User
from middlewares.rate_limit import limit_ip
from services.auth.email_sender import otp_email, send_email
from services.auth.otp import issue_otp, verify_otp
from services.auth.passwords import hash_password, validate_email, validate_password
from services.auth.sessions import create_session, optional_user
from services.business.access import Ctx, draft_ctx
from services.payments import pricing
from services.payments.service import create_subscription_order
from services.signup.service import apply_wizard, catalog, check_slug, validate_wizard
from services.storage.logos import save_logo

router = APIRouter(prefix="/api/signup", tags=["signup"])
log = get_logger("signup")


class StartIn(BaseModel):
    name: str
    email: str
    phone: str = ""
    password: str
    confirm: str
    terms: bool


class EmailIn(BaseModel):
    email: str


class VerifyIn(BaseModel):
    email: str
    code: str


class DraftIn(BaseModel):
    step: int = 1
    state: dict


class CheckoutIn(BaseModel):
    state: dict


@router.get("/catalog")
def get_catalog():
    return catalog()


@router.get("/pricing")
def get_pricing(db: Session = Depends(get_db), user: User | None = Depends(optional_user)):
    first = pricing.is_first_time_top(db, user.id) if user else True
    return pricing.public_table(first_time_top=first)


@router.get("/slug-available")
def slug_available(slug: str, request: Request, db: Session = Depends(get_db),
                   user: User | None = Depends(optional_user)):
    limit_ip(request, "slug", 60, 60)
    own = None
    if user:
        m = db.scalars(select(Membership).where(Membership.user_id == user.id)).first()
        own = m.business_id if m else None
    try:
        check_slug(db, slug, own)
        return {"available": True}
    except AppError as e:
        return {"available": False, "message": e.message}


@router.post("/start")
def start(body: StartIn, request: Request, db: Session = Depends(get_db)):
    limit_ip(request, "signup-start", 10, 600)
    name = body.name.strip()
    if not name:
        raise bad_request("Enter your full name.")
    email = validate_email(body.email)
    phone = body.phone.strip()
    if phone:
        digits = "".join(c for c in phone if c.isdigit())
        if not 10 <= len(digits) <= 13:
            raise bad_request("Enter a valid phone number, or leave it empty.")
    validate_password(body.password, body.confirm)
    if not body.terms:
        raise bad_request("Please accept the Terms of Service and Privacy Policy.")
    if db.scalars(select(User).where(User.email == email)).first():
        raise conflict("An account with this email already exists. Sign in, or reset your password.",
                       code="email_exists")
    pending = db.scalars(select(PendingSignup).where(PendingSignup.email == email)).first()
    if pending is None:
        pending = PendingSignup(email=email, name=name, phone=phone, password_hash="")
        db.add(pending)
    pending.name, pending.phone, pending.password_hash = name[:120], phone[:32], hash_password(body.password)
    code = issue_otp(db, "signup", email)
    subject, text = otp_email(code, "signup")
    send_email(email, subject, text)
    db.commit()
    log.info("signup otp sent to=%s", mask_email(email))
    return {"ok": True}


@router.post("/resend")
def resend(body: EmailIn, request: Request, db: Session = Depends(get_db)):
    limit_ip(request, "signup-resend", 10, 600)
    email = validate_email(body.email)
    if db.scalars(select(PendingSignup).where(PendingSignup.email == email)).first() is None:
        raise not_found("Start sign-up again to get a code.")
    code = issue_otp(db, "signup", email)
    subject, text = otp_email(code, "signup")
    send_email(email, subject, text)
    db.commit()
    return {"ok": True}


@router.post("/verify")
def verify(body: VerifyIn, request: Request, response: Response, db: Session = Depends(get_db)):
    limit_ip(request, "signup-verify", 20, 600)
    email = validate_email(body.email)
    pending = db.scalars(select(PendingSignup).where(PendingSignup.email == email)).first()
    if pending is None:
        raise not_found("Start sign-up again to get a code.")
    verify_otp(db, "signup", email, body.code)
    if db.scalars(select(User).where(User.email == email)).first():
        raise conflict("An account with this email already exists. Sign in instead.", code="email_exists")
    user = User(email=email, name=pending.name, phone=pending.phone, password_hash=pending.password_hash,
                email_verified=True)
    db.add(user)
    db.flush()
    biz = Business(owner_user_id=user.id, status="draft", working_days=["Mon", "Tue", "Wed", "Thu", "Fri", "Sat"])
    db.add(biz)
    db.flush()
    db.add(Membership(user_id=user.id, business_id=biz.id, role="owner"))
    db.delete(pending)
    create_session(db, response, user)
    db.commit()
    log.info("signup verified user=%s business=%s", user.id, biz.id)
    return {"ok": True, "owner": {"name": user.name, "email": user.email, "phone": user.phone}}


@router.get("/draft")
def get_draft(ctx: Ctx = Depends(draft_ctx)):
    biz = ctx.business
    return {
        "status": biz.status,
        "owner": {"name": ctx.user.name, "email": ctx.user.email, "phone": ctx.user.phone},
        "draft": biz.signup_draft or {},
        "logo_url": biz.logo_url,
        "redirect": "/dashboard" if biz.status != "draft" else None,
    }


@router.put("/draft")
def save_draft(body: DraftIn, ctx: Ctx = Depends(draft_ctx)):
    if ctx.business.status != "draft":
        return {"ok": True, "redirect": "/dashboard"}
    state = dict(body.state)
    state.pop("owner", None)  # never store passwords in the draft
    ctx.business.signup_draft = {"step": max(1, min(int(body.step), 5)), "state": state}
    ctx.db.commit()
    return {"ok": True}


@router.post("/logo")
async def upload_logo(file: UploadFile = File(...), ctx: Ctx = Depends(draft_ctx)):
    data = await file.read(1_000_001)
    ctx.business.logo_url = save_logo(ctx.business.id, data)
    ctx.db.commit()
    return {"ok": True, "logo_url": ctx.business.logo_url}


@router.post("/checkout")
def checkout(body: CheckoutIn, request: Request, ctx: Ctx = Depends(draft_ctx)):
    limit_ip(request, "signup-checkout", 20, 600)
    db, biz = ctx.db, ctx.business
    if biz.status != "draft":
        raise conflict("This business is already active. Renew from the dashboard.", redirect="/dashboard")
    clean = validate_wizard(db, biz, body.state)
    apply_wizard(db, biz, clean)
    q = pricing.quote(db, ctx.user.id, clean["plan"], clean["billing"])
    order = create_subscription_order(db, biz, ctx.user.id, q, utcnow())
    state = dict(body.state)
    state.pop("owner", None)
    biz.signup_draft = {"step": 5, "state": state}
    db.commit()
    return {"ok": True, "order": order, "label": q.label}
