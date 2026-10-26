"""
services/notifications/service.py
---------------------------------
Two channels for Base (decided): the dashboard bell (a Notification row,
pushed live over server-sent events) and an email to the owner for new
bookings and cancellations.

Also sends the "Top customer paid" alert to ALERT_EMAIL (RR) so he can
call them and set up Twilio by hand.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app_platform.config import get_settings
from app_platform.timeutil import fmt_local
from dbhandlers.models import Business, Notification, User
from services.auth.email_sender import send_email


def notify(db: Session, biz: Business, type_: str, title: str, body: str = "", *, email_owner: bool = False) -> None:
    db.add(Notification(business_id=biz.id, type=type_, title=title[:120], body=body[:255]))
    db.flush()
    if email_owner:
        owner = db.get(User, biz.owner_user_id)
        if owner:
            send_email(owner.email, f"CENTRA: {title}", f"{title}\n\n{body}\n\nOpen your dashboard to see details.",
                       critical=False)


def booking_line(booking, customer_name: str, items: list) -> str:
    services = ", ".join(i.name for i in items) or "Service"
    return f"{customer_name} · {services} · {fmt_local(booking.start_at)}"


def alert_top_payment(db: Session, biz: Business, sub) -> None:
    s = get_settings()
    owner = db.get(User, biz.owner_user_id)
    body = "\n".join([
        "A business just paid for the Top plan. WhatsApp setup is waiting for you.",
        "",
        f"Business:   {biz.name}",
        f"Type:       {biz.business_type} ({biz.category})",
        f"City:       {biz.city}",
        f"Owner:      {owner.name if owner else ''}",
        f"Owner email:{' ' + owner.email if owner else ''}",
        f"Owner phone:{' ' + owner.phone if owner and owner.phone else ' (not given)'}",
        f"Business phone: {biz.phone}",
        f"WhatsApp number: {biz.whatsapp or '(not given)'}",
        f"Plan:       {sub.plan} · {sub.billing} · {sub.months} month(s)"
        + (f" + {sub.free_months} free" if sub.free_months else ""),
        f"Booking page: {s.base_url}/{biz.slug}",
    ])
    send_email(s.alert_email, f"CENTRA Top setup needed: {biz.name}", body, critical=False)
