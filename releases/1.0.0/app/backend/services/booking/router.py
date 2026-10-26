"""
services/booking/router.py
--------------------------
Customer-facing API. No customer accounts: a booking is managed through
a secret link that works only for that booking and expires at the
appointment time. A lost link is recovered with the booking code plus a
one-time code sent to the customer's email.

GET  /api/public/businesses/{slug}
GET  /api/public/businesses/{slug}/availability?date=&services=1,2&staff=any|id
POST /api/public/businesses/{slug}/bookings
POST /api/public/bookings/pay              (retry deposit payment with the manage token)
GET  /api/public/manage?token=
POST /api/public/manage/cancel
POST /api/public/manage/reschedule
POST /api/public/manage/recover/request    email + booking code -> OTP email
POST /api/public/manage/recover/verify     -> fresh manage link
POST /api/internal/send-reminders          cron job (needs CRON_SECRET)
"""
from __future__ import annotations

import hmac
from datetime import date, timedelta

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app_platform.config import get_settings
from app_platform.errors import AppError, bad_request, not_found
from app_platform.timeutil import fmt_local, label_12h
from dbhandlers.database import get_db, utcnow
from dbhandlers.models import Booking, Business, Customer, Service, Staff
from middlewares.rate_limit import limit_ip
from services.auth.email_sender import otp_email, send_email
from services.auth.otp import issue_otp, verify_otp
from services.auth.passwords import normalize_email
from services.booking import engine

router = APIRouter(tags=["public"])


def _active_business(db: Session, slug: str) -> Business:
    biz = db.scalars(select(Business).where(Business.slug == slug.lower())).first()
    if biz is None or biz.status != "active":
        raise not_found("This booking page isn't available.")
    return biz


def _initials(name: str) -> str:
    parts = [p for p in name.split() if p]
    return "".join(p[0] for p in parts[:2]).upper() or "•"


def _terms(biz: Business) -> str:
    deposit = (f"A deposit of {biz.deposit_percent}% of the booking total holds your slot. Cancel at least "
               f"{biz.refund_window_hours} hours before your appointment for a full deposit refund; later "
               "cancellations forfeit the deposit. " if biz.deposits_enabled else "")
    return (deposit + "If the business has to close on a day you have booked, it will contact you directly, or "
            f"you can call it on {biz.phone}. CENTRA provides the booking software only and is not responsible "
            "for a business closing on a booked day. Phone numbers are not verified; a wrong or fake number is the "
            "customer's responsibility.")


def public_business(db: Session, biz: Business, now) -> dict:
    today = engine.today_local(now)
    lo, hi = engine.window(now)
    services = db.scalars(select(Service).where(Service.business_id == biz.id, Service.shown.is_(True))
                          .order_by(Service.sort, Service.id)).all()
    staff = db.scalars(select(Staff).where(Staff.business_id == biz.id, Staff.shown.is_(True)).order_by(Staff.id)).all()
    return {
        "slug": biz.slug, "name": biz.name, "address": ", ".join(x for x in [biz.address, biz.city] if x),
        "phone": biz.phone, "logo": biz.logo_url, "type": biz.business_type,
        "hours": f"{label_12h(biz.open_time)} – {label_12h(biz.close_time)}",
        "days": biz.working_days or [], "prices": biz.show_pricing, "allowMultiple": biz.allow_multiple,
        "depositEnabled": biz.deposits_enabled, "depositPercent": biz.deposit_percent,
        "refundWindowHours": biz.refund_window_hours,
        "services": [{"id": s.id, "name": s.name, "description": s.description, "minutes": s.duration_min,
                      "price": (s.price_paise / 100) if biz.show_pricing else None,
                      "depositBase": s.price_paise / 100}
                     for s in services if not engine.suspended_on(s, today)],
        "staff": [{"id": "any", "name": "Any available", "role": "Earliest suitable appointment", "initials": "✦"}] + [
            {"id": st.id, "name": st.name, "role": st.role, "initials": _initials(st.name)}
            for st in staff if not engine.suspended_on(st, today)] if staff else [],
        "window": {"from": lo.isoformat(), "to": hi.isoformat()},
        "closedDates": engine.closed_dates(db, biz, now),
        "terms": _terms(biz),
    }


class BookIn(BaseModel):
    services: list[int]
    staff: str = "any"
    date: date
    time: str
    name: str
    email: str
    phone: str
    note: str = ""


class TokenIn(BaseModel):
    token: str


class RescheduleIn(BaseModel):
    token: str
    date: date
    time: str


class RecoverIn(BaseModel):
    email: str
    code: str


class RecoverVerifyIn(BaseModel):
    email: str
    code: str
    otp: str


@router.get("/api/public/businesses/{slug}")
def get_business(slug: str, db: Session = Depends(get_db)):
    now = utcnow()
    biz = _active_business(db, slug)
    data = public_business(db, biz, now)
    db.commit()
    return data


@router.get("/api/public/businesses/{slug}/availability")
def get_availability(slug: str, date: date, services: str, request: Request, staff: str = "any",
                     db: Session = Depends(get_db)):
    limit_ip(request, "availability", 120, 60)
    biz = _active_business(db, slug)
    ids = [int(x) for x in services.split(",") if x.strip().isdigit()]
    data = engine.availability(db, biz, ids, staff, date, utcnow())
    db.commit()
    return data


@router.post("/api/public/businesses/{slug}/bookings")
def book(slug: str, body: BookIn, request: Request, db: Session = Depends(get_db)):
    limit_ip(request, "book", 20, 600)
    now = utcnow()
    biz = _active_business(db, slug)
    nb = engine.NewBooking(service_ids=body.services, staff=body.staff, date=body.date, time=body.time,
                           name=body.name, email=body.email, phone=body.phone, note=body.note)
    b, token = engine.create_booking(db, biz, nb, now)
    payment = engine.after_create(db, biz, b, token, now)
    db.commit()
    s = get_settings()
    return {"ok": True, "booking": engine.booking_public_view(db, biz, b), "token": token,
            "manageUrl": f"{s.base_url}/manage?token={token}", "payment": payment}


@router.post("/api/public/bookings/pay")
def retry_pay(body: TokenIn, db: Session = Depends(get_db)):
    now = utcnow()
    b = engine.booking_by_token(db, body.token, now)
    biz = db.get(Business, b.business_id)
    payment = engine.deposit_order(db, biz, b, now)
    db.commit()
    return {"ok": True, "payment": payment}


def _manage_payload(db: Session, b: Booking) -> dict:
    biz = db.get(Business, b.business_id)
    return {"booking": engine.booking_public_view(db, biz, b), "business": public_business(db, biz, utcnow())}


@router.get("/api/public/manage")
def manage(token: str, db: Session = Depends(get_db)):
    b = engine.booking_by_token(db, token, utcnow())
    engine.expire_holds(db, b.business_id, utcnow())
    data = _manage_payload(db, b)
    db.commit()
    return data


@router.post("/api/public/manage/cancel")
def manage_cancel(body: TokenIn, db: Session = Depends(get_db)):
    now = utcnow()
    b = engine.booking_by_token(db, body.token, now)
    biz = db.get(Business, b.business_id)
    result = engine.cancel_booking(db, biz, b, "customer", now)
    db.commit()
    return {"ok": True, **result, **_manage_payload(db, b)}


@router.post("/api/public/manage/reschedule")
def manage_reschedule(body: RescheduleIn, db: Session = Depends(get_db)):
    now = utcnow()
    b = engine.booking_by_token(db, body.token, now)
    biz = db.get(Business, b.business_id)
    engine.reschedule_booking(db, biz, b, body.date, body.time, now, by="customer")
    db.commit()
    return {"ok": True, **_manage_payload(db, b)}


def _booking_for_recovery(db: Session, email: str, code: str) -> Booking:
    b = db.scalars(select(Booking).where(Booking.code == code.strip().upper())).first()
    if b is not None:
        c = db.get(Customer, b.customer_id)
        if c and c.email == email:
            return b
    raise not_found("We couldn't find a booking with that email and booking code.")


@router.post("/api/public/manage/recover/request")
def recover_request(body: RecoverIn, request: Request, db: Session = Depends(get_db)):
    limit_ip(request, "recover", 10, 600)
    email = normalize_email(body.email)
    b = _booking_for_recovery(db, email, body.code)
    if b.start_at <= utcnow():
        raise AppError(410, "link_expired", "This appointment time has passed.")
    otp = issue_otp(db, "manage_booking", email, ref=b.code)
    subject, text = otp_email(otp, "manage_booking")
    send_email(email, subject, text)
    db.commit()
    return {"ok": True}


@router.post("/api/public/manage/recover/verify")
def recover_verify(body: RecoverVerifyIn, request: Request, db: Session = Depends(get_db)):
    limit_ip(request, "recover-verify", 20, 600)
    email = normalize_email(body.email)
    b = _booking_for_recovery(db, email, body.code)
    verify_otp(db, "manage_booking", email, body.otp, ref=b.code)
    token = engine.manage_token(b)
    db.commit()
    return {"ok": True, "token": token, "manageUrl": f"{get_settings().base_url}/manage?token={token}"}


@router.post("/api/internal/send-reminders")
def send_reminders(db: Session = Depends(get_db), x_cron_secret: str = Header(default="")):
    """Call hourly from a Render cron job: emails customers whose appointment is 20–28 hours away."""
    secret = get_settings().cron_secret
    if not secret or not hmac.compare_digest(secret, x_cron_secret):
        raise not_found()
    now = utcnow()
    rows = db.scalars(select(Booking).where(
        Booking.status == "confirmed", Booking.reminder_sent.is_(False),
        Booking.start_at > now + timedelta(hours=20), Booking.start_at <= now + timedelta(hours=28),
    )).all()
    sent = 0
    for b in rows:
        biz = db.get(Business, b.business_id)
        c = db.get(Customer, b.customer_id)
        token = engine.manage_token(b)
        link = f"{get_settings().base_url}/manage?token={token}"
        if c and send_email(c.email, f"Reminder: {biz.name} tomorrow",
                            f"This is a reminder of your booking {b.code} at {biz.name} on {fmt_local(b.start_at)}.\n\n"
                            f"Address: {biz.address}, {biz.city}\nPhone: {biz.phone}\n\nManage your booking: {link}",
                            critical=False):
            b.reminder_sent = True
            sent += 1
    db.commit()
    return {"ok": True, "sent": sent}


def bad_slug_guard(slug: str) -> None:
    if not slug or len(slug) > 60:
        raise bad_request("Invalid link.")
