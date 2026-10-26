"""
services/business/state.py
--------------------------
Builds the management page's `state` object from the database, in the
same shape the CENTRA prototype used, so the page's own rendering code is
reused almost unchanged.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app_platform.config import get_settings
from app_platform.timeutil import WEEKDAY_NAMES, to_local
from dbhandlers.models import (
    Booking, Business, Closure, Customer, Notification, Service, Staff, StaffLeave, Subscription,
)
from services.booking.engine import DEPOSIT_LABELS, STATUS_LABELS, expire_holds, today_local
from services.business.closures import festivals, missing_festival_years
from services.business.areas import areas_limit
from services.reports.service import payments_summary, reports


def _suspend_value(obj) -> str:
    if obj.suspended_forever:
        return "forever"
    return obj.suspended_until.isoformat() if obj.suspended_until else ""


def _when(created: datetime, now: datetime) -> str:
    delta = now - created
    if delta < timedelta(minutes=1):
        return "Just now"
    if delta < timedelta(hours=1):
        return f"{int(delta.total_seconds() // 60)} min ago"
    if delta < timedelta(days=1):
        return f"{int(delta.total_seconds() // 3600)} hr ago"
    return to_local(created).strftime("%d %b, %I:%M %p")


def closure_dict(c: Closure) -> dict:
    out = {"id": c.id, "type": c.type, "label": c.label, "note": c.note}
    if c.type == "range":
        out.update(start=c.start_date.isoformat() if c.start_date else "", end=c.end_date.isoformat() if c.end_date else "")
    elif c.type == "weekly":
        out["weekday"] = WEEKDAY_NAMES[c.weekday] if c.weekday is not None else ""
    elif c.type == "monthly":
        out["day"] = c.day_of_month
    elif c.type == "yearly":
        out.update(month=c.month, day=c.day_of_month)
    elif c.type == "festival":
        out["festival"] = c.festival_key
        out["festivalLabel"] = festivals().get(c.festival_key, {}).get("label", c.festival_key)
    return out


def build_state(db: Session, biz: Business, now: datetime) -> dict:
    expire_holds(db, biz.id, now)
    db.commit()
    s = get_settings()
    today = today_local(now)
    services = db.scalars(select(Service).where(Service.business_id == biz.id).order_by(Service.sort, Service.id)).all()
    staff = db.scalars(select(Staff).where(Staff.business_id == biz.id).order_by(Staff.id)).all()
    leaves = defaultdict(list)
    for lv in db.scalars(select(StaffLeave).where(StaffLeave.business_id == biz.id, StaffLeave.leave_date >= today)
                         .order_by(StaffLeave.leave_date)).all():
        leaves[lv.staff_id].append({"date": lv.leave_date.isoformat(), "reason": lv.reason})
    staff_names = {st.id: st.name for st in staff}

    bookings = db.scalars(select(Booking).where(
        Booking.business_id == biz.id, Booking.local_date >= today - timedelta(days=30),
    ).order_by(Booking.start_at)).all()
    customers = {c.id: c for c in db.scalars(select(Customer).where(Customer.business_id == biz.id)).all()}
    appts = []
    count_by_customer = defaultdict(int)
    last_by_customer: dict[int, date] = {}
    for b in bookings:
        c = customers.get(b.customer_id)
        loc = to_local(b.start_at)
        if b.status not in ("expired",):
            count_by_customer[b.customer_id] += 1
            last_by_customer[b.customer_id] = max(last_by_customer.get(b.customer_id, b.local_date), b.local_date)
        appts.append({
            "id": b.id, "code": b.code, "date": loc.date().isoformat(), "time": loc.strftime("%H:%M"),
            "customer": c.name if c else "", "phone": c.phone if c else "", "email": c.email if c else "",
            "service": ", ".join(i.name for i in b.items), "staff": staff_names.get(b.staff_id, "Unassigned"),
            "staffId": b.staff_id, "status": STATUS_LABELS.get(b.status, b.status), "statusCode": b.status,
            "deposit": DEPOSIT_LABELS.get(b.deposit_status, b.deposit_status), "note": b.note,
            "source": b.source,
        })
    all_counts = defaultdict(int)
    all_last: dict[int, date] = {}
    for cid, d in db.execute(select(Booking.customer_id, Booking.local_date).where(
            Booking.business_id == biz.id, Booking.status != "expired")).all():
        all_counts[cid] += 1
        all_last[cid] = max(all_last.get(cid, d), d)
    cust_list = sorted(
        [{"id": c.id, "name": c.name, "phone": c.phone, "email": c.email, "bookings": all_counts.get(c.id, 0),
          "last": all_last[c.id].strftime("%b %d, %Y") if c.id in all_last else "", "notes": c.notes}
         for c in customers.values()],
        key=lambda x: x["last"], reverse=True)

    notes = db.scalars(select(Notification).where(Notification.business_id == biz.id)
                       .order_by(Notification.id.desc()).limit(50)).all()
    closures = db.scalars(select(Closure).where(Closure.business_id == biz.id).order_by(Closure.id)).all()
    subs = db.scalars(select(Subscription).where(Subscription.business_id == biz.id, Subscription.status == "active")
                      .order_by(Subscription.id.desc())).all()
    billing_rows = [{
        "invoice": f"CTR-{sub.id:05d}", "date": to_local(sub.created_at).strftime("%b %d, %Y"),
        "plan": f"{sub.plan} · {'Monthly' if sub.billing == 'monthly' else '6-month pack'}",
        "amount": sub.amount_paise / 100, "status": "Paid",
        "period": f"until {sub.period_end.strftime('%d %b %Y')}" if sub.period_end else "",
    } for sub in subs]
    years = sorted({today.year, (today + timedelta(days=62)).year})
    return {
        "business": {
            "id": biz.id, "name": biz.name, "city": biz.city, "address": biz.address, "phone": biz.phone,
            "whatsapp": biz.whatsapp, "slug": biz.slug or "", "areas": biz.areas or [], "days": biz.working_days or [],
            "open": biz.open_time, "close": biz.close_time, "showPricing": biz.show_pricing,
            "allowMultiple": biz.allow_multiple, "logo": biz.logo_url, "type": biz.business_type,
            "category": biz.category,
        },
        "bookingUrl": f"{s.base_url}/{biz.slug}", "domainLabel": s.public_domain_label,
        "areasLimit": areas_limit(),
        "today": today.isoformat(),
        "services": [{
            "id": x.id, "name": x.name, "price": x.price_paise / 100, "duration": x.duration_min,
            "interval": x.interval_min, "shown": x.shown, "suspend": _suspend_value(x),
        } for x in services],
        "staff": [{
            "id": x.id, "name": x.name, "role": x.role, "days": x.working_days or [], "start": x.start_time,
            "end": x.end_time, "bs": x.break_start, "be": x.break_end, "interval": x.interval_min or "",
            "shown": x.shown, "suspend": _suspend_value(x), "leave": leaves.get(x.id, []),
        } for x in staff],
        "appointments": appts,
        "customers": cust_list,
        "closures": [closure_dict(c) for c in closures],
        "festivals": [{"key": k, "label": v.get("label", k)} for k, v in festivals().items()],
        "festivalWarnings": missing_festival_years(closures, years),
        "notifications": [{
            "id": n.id, "type": n.type, "title": n.title, "body": n.body, "when": _when(n.created_at, now),
            "unread": n.unread,
        } for n in notes],
        "deposits": biz.deposits_enabled, "depositPercent": biz.deposit_percent,
        "refundWindow": str(biz.refund_window_hours),
        "plan": biz.plan, "billing": "Monthly" if biz.billing == "monthly" else "6-month pack",
        "renewal": biz.plan_active_until.strftime("%b %d, %Y") if biz.plan_active_until else "",
        "planActive": bool(biz.plan_active_until and biz.plan_active_until > now),
        "topSetup": biz.top_setup_status,
        "billingHistory": billing_rows,
        "sound": biz.sound_alerts,
        "payments": payments_summary(db, biz.id, now),
        "reports": reports(db, biz.id, now),
        "supportEmail": s.support_email,
        "lastNotificationId": notes[0].id if notes else 0,
    }
