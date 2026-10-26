"""
services/payments/service.py
----------------------------
What happens when money arrives. Two server-side confirmations exist and
both lead to mark_paid():

1. /api/payments/verify: the browser hands back Razorpay's
   (order_id, payment_id, signature). The signature is checked with our
   secret AND the payment is fetched from Razorpay to confirm it was
   captured. The browser's word alone is never trusted.
2. /api/payments/webhook: Razorpay calls us directly; its signature is
   checked with the webhook secret. Duplicate events are ignored.

mark_paid() is idempotent: the second confirmation of the same payment
does nothing.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app_platform.errors import AppError, not_found
from app_platform.logger import get_logger
from app_platform.timeutil import add_months_dt
from dbhandlers.models import Business, Payment, Subscription
from services.booking import engine
from services.notifications.service import alert_top_payment, notify
from services.payments import gateway

log = get_logger("payments")


def mark_paid(db: Session, order_id: str, payment_id: str, now: datetime) -> dict:
    p = db.scalars(select(Payment).where(Payment.order_id == order_id)).first()
    if p is None:
        raise not_found("Payment not found.")
    if p.status in ("paid", "refunded"):
        return {"kind": p.kind, "status": "already_processed", "business_id": p.business_id}
    p.status, p.payment_id, p.paid_at = "paid", payment_id, now
    db.flush()
    if p.kind == "subscription":
        activate_subscription(db, p, now)
        result = "active"
    else:
        result = engine.confirm_deposit(db, p, now)
    log.info("payment confirmed kind=%s business=%s result=%s", p.kind, p.business_id, result)
    return {"kind": p.kind, "status": result, "business_id": p.business_id}


def verify_and_mark(db: Session, order_id: str, payment_id: str, signature: str, now: datetime) -> dict:
    if not gateway.verify_checkout_signature(order_id, payment_id, signature):
        raise AppError(400, "bad_signature", "We couldn't verify this payment. If money was taken, it will be "
                                             "confirmed automatically within a few minutes.")
    info = gateway.fetch_payment(payment_id)
    if info.get("status") not in ("captured", "authorized") or info.get("order_id", order_id) != order_id:
        raise AppError(400, "not_captured", "The payment isn't complete yet. If money was taken, it will be "
                                            "confirmed automatically within a few minutes.")
    return mark_paid(db, order_id, payment_id, now)


def activate_subscription(db: Session, p: Payment, now: datetime) -> None:
    sub = db.get(Subscription, p.subscription_id)
    biz = db.get(Business, p.business_id)
    if sub is None or biz is None or sub.status == "active":
        return
    start = biz.plan_active_until if (biz.plan_active_until and biz.plan_active_until > now) else now
    sub.period_start = start
    sub.period_end = add_months_dt(start, sub.months + sub.free_months)
    sub.status = "active"
    was_draft = biz.status == "draft"
    biz.plan, biz.billing = sub.plan, sub.billing
    biz.plan_active_until = sub.period_end
    biz.status = "active"
    if was_draft:
        biz.signup_draft = {}
    notify(db, biz, "System", "Plan active" if was_draft else "Plan renewed",
           f"{sub.plan} plan · active until {sub.period_end.strftime('%d %b %Y')}")
    if sub.plan == "Top" and biz.top_setup_status != "done":
        biz.top_setup_status = "pending"
        alert_top_payment(db, biz, sub)
    db.flush()


def create_subscription_order(db: Session, biz: Business, owner_user_id: int, q, now: datetime) -> dict:
    sub = Subscription(business_id=biz.id, owner_user_id=owner_user_id, plan=q.plan, billing=q.billing,
                       amount_paise=q.amount_paise, months=q.months, free_months=q.free_months)
    db.add(sub)
    db.flush()
    order_id = gateway.create_order(q.amount_paise, receipt=f"sub-{sub.id}",
                                    notes={"kind": "subscription", "business": str(biz.id), "plan": q.plan})
    db.add(Payment(business_id=biz.id, kind="subscription", subscription_id=sub.id, order_id=order_id,
                   amount_paise=q.amount_paise))
    db.flush()
    return {"order_id": order_id, "amount": q.amount_paise, "currency": "INR", "key": gateway.public_key(),
            "name": "CENTRA", "description": f"{q.plan} plan · {q.billing}", "mock": gateway.is_mock(),
            "plan": q.plan, "billing": q.billing, "months": q.months, "free_months": q.free_months}


def handle_webhook(db: Session, event: dict, event_id: str, now: datetime) -> dict:
    from dbhandlers.models import WebhookEvent

    if event_id:
        if db.scalars(select(WebhookEvent).where(WebhookEvent.event_id == event_id)).first():
            return {"ok": True, "duplicate": True}
        db.add(WebhookEvent(event_id=event_id))
        db.flush()
    kind = event.get("event", "")
    payload = event.get("payload", {})
    if kind in ("payment.captured", "order.paid"):
        pay = payload.get("payment", {}).get("entity", {})
        order_id = pay.get("order_id") or payload.get("order", {}).get("entity", {}).get("id")
        if order_id and pay.get("id"):
            try:
                mark_paid(db, order_id, pay["id"], now)
            except AppError:
                log.warning("webhook for unknown order ignored")
    elif kind == "payment.failed":
        pay = payload.get("payment", {}).get("entity", {})
        p = db.scalars(select(Payment).where(Payment.order_id == pay.get("order_id"))).first()
        if p and p.status == "created":
            p.status = "failed"
    return {"ok": True}
