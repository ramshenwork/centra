"""
services/reports/service.py
---------------------------
Numbers for the Reports and Payments pages, computed live from bookings
and payments (no separate reporting tables to keep in sync).
Reports cover the last 30 days of appointment dates.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app_platform.timeutil import WEEKDAYS, to_local
from dbhandlers.models import Booking, Customer, Payment

CANCELLED = ("cancelled", "refunded", "deposit_forfeited")


def reports(db: Session, business_id: int, now: datetime) -> dict:
    since = (now - timedelta(days=30)).date()
    rows = db.scalars(select(Booking).where(Booking.business_id == business_id, Booking.local_date >= since,
                                            Booking.local_date <= now.date() + timedelta(days=1))).all()
    real = [b for b in rows if b.status not in ("expired", "awaiting_payment")]
    total = len(real)

    def pct(n: int) -> str:
        return f"{(n / total * 100):.1f}% of bookings" if total else "No bookings yet"

    cancelled = sum(1 for b in real if b.status in CANCELLED)
    noshow = sum(1 for b in real if b.status == "no_show")
    completed = sum(1 for b in real if b.status == "completed")
    by_day = Counter(WEEKDAYS[b.local_date.weekday()] for b in real if b.status not in CANCELLED)
    peak = max(by_day.values()) if by_day else 0
    busiest = [[d, round(by_day.get(d, 0) / peak * 100) if peak else 0, by_day.get(d, 0)] for d in WEEKDAYS]
    svc = Counter(i.name for b in real if b.status not in CANCELLED for i in b.items)
    top = svc.most_common(4)
    top_max = top[0][1] if top else 0
    top_services = [[n, c, round(c / top_max * 100) if top_max else 0] for n, c in top]
    per_customer = Counter(b.customer_id for b in real)
    repeat = sum(1 for c in per_customer.values() if c >= 2)
    return {
        "bookings": total, "cancellations": cancelled, "noShows": noshow, "completed": completed,
        "cancellationsNote": pct(cancelled), "noShowsNote": pct(noshow), "completedNote": pct(completed),
        "busiest": busiest, "topServices": top_services, "repeatCustomers": repeat,
        "customersInPeriod": len(per_customer),
    }


def payments_summary(db: Session, business_id: int, now: datetime) -> dict:
    pays = db.scalars(select(Payment).where(Payment.business_id == business_id, Payment.kind == "deposit")
                      .order_by(Payment.created_at.desc())).all()
    total = sum(p.amount_paise for p in pays if p.status in ("paid", "refunded"))
    refunds = sum(p.amount_paise for p in pays if p.status == "refunded")
    held = 0
    payouts = 0
    recent = []
    for p in pays[:25]:
        b = db.get(Booking, p.booking_id) if p.booking_id else None
        c = db.get(Customer, b.customer_id) if b else None
        if p.status == "paid" and b is not None:
            if b.start_at > now and b.status == "confirmed":
                held += p.amount_paise
            else:
                payouts += p.amount_paise
        recent.append({
            "customer": c.name if c else "", "type": "Deposit",
            "amount": p.amount_paise / 100,
            "status": {"created": "Pending", "paid": "Paid", "failed": "Failed", "refunded": "Refunded"}.get(p.status, p.status),
            "payout": {"route_held": "Route (held)", "manual_pending": "Manual payout"}.get(p.payout_status, ""),
            "when": to_local(p.created_at).strftime("%d %b %Y"),
        })
    return {"total": total / 100, "held": held / 100, "refunds": refunds / 100, "payouts": payouts / 100,
            "recent": recent}
