"""
services/booking/engine.py
--------------------------
The single source of truth for bookings. The public booking page, the
customer's manage link and the owner's dashboard all go through these
functions, so they can never disagree about what is free.

Key rules:
- Booking window: today through the same date 2 months ahead (rolling).
- Closed if not a working day, or any closure rule matches.
- A displayed slot is not a booking: create_booking() re-checks the slot
  under a per-business lock before saving it, so two people can't take
  the same slot at the same moment.
- With deposits on, the booking waits as "awaiting_payment" and holds the
  slot for BOOKING_HOLD_MINUTES (10). An unpaid hold expires on its own.
  A payment that arrives after the hold expired is confirmed only if the
  slot is still free; otherwise it is refunded automatically.
- Customer cancels at least `refund_window_hours` before the start: full
  deposit refund. Later: deposit forfeited. The owner cancelling always
  refunds the deposit.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import math
import secrets
import threading
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app_platform.config import get_settings
from app_platform.errors import AppError, bad_request, conflict, not_found, too_many
from app_platform.logger import get_logger, mask_email
from app_platform.timeutil import (
    WEEKDAYS, booking_window, fmt_local, local_now, local_to_utc, to_local, to_minutes,
)
from dbhandlers.database import is_postgres
from dbhandlers.models import (
    Booking, BookingItem, Business, Closure, Customer, Payment, Service, Staff, StaffLeave,
)
from services.auth.passwords import token_hash, validate_email
from services.booking.availability import (
    DayInput, StaffDay, compute_slots, interval_and_duration, pick_staff, to_min,
)
from services.business.closures import closed_by_rules
from services.payments import gateway

log = get_logger("booking")

BLOCKING = ("requested", "confirmed")
STATUS_LABELS = {
    "awaiting_payment": "Awaiting payment", "requested": "Requested", "confirmed": "Confirmed",
    "completed": "Completed", "cancelled": "Cancelled", "rejected": "Rejected", "expired": "Expired",
    "no_show": "No-show", "refunded": "Refunded", "deposit_forfeited": "Deposit forfeited",
}
DEPOSIT_LABELS = {
    "not_required": "Not required", "pending": "Pending", "paid": "Paid", "refunded": "Refunded",
    "forfeited": "Forfeited", "expired": "Expired",
}
_locks: dict[int, threading.Lock] = defaultdict(threading.Lock)
_locks_guard = threading.Lock()
_CODE_CHARS = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def _business_lock(business_id: int) -> threading.Lock:
    with _locks_guard:
        return _locks[business_id]


def _lock_business_row(db: Session, business_id: int) -> None:
    """Cross-process lock on Postgres (Supabase); SQLite already serialises writes."""
    if is_postgres():
        db.execute(select(Business.id).where(Business.id == business_id).with_for_update())


# ------------------------------------------------------------------ helpers
def suspended_on(obj, d: date) -> bool:
    return bool(obj.suspended_forever or (obj.suspended_until and d <= obj.suspended_until))


def today_local(now: datetime) -> date:
    return local_now(now).date()


def window(now: datetime) -> tuple[date, date]:
    return booking_window(today_local(now), get_settings().booking_window_months)


def is_open_day(biz: Business, closures: list, d: date) -> bool:
    return WEEKDAYS[d.weekday()] in (biz.working_days or []) and not closed_by_rules(closures, d)


def expire_holds(db: Session, business_id: int, now: datetime) -> None:
    rows = db.scalars(select(Booking).where(
        Booking.business_id == business_id, Booking.status == "awaiting_payment",
        Booking.hold_expires_at <= now,
    )).all()
    for b in rows:
        b.status = "expired"
        b.deposit_status = "expired"
    if rows:
        db.flush()


def _blocking_filter(now: datetime):
    return or_(Booking.status.in_(BLOCKING),
               (Booking.status == "awaiting_payment") & (Booking.hold_expires_at > now))


def bookable_services(db: Session, biz: Business, ids: list[int], d: date) -> list[Service]:
    if not ids:
        raise bad_request("Choose at least one service.")
    if not biz.allow_multiple and len(ids) > 1:
        raise bad_request("This business takes one service per booking.")
    rows = db.scalars(select(Service).where(Service.business_id == biz.id, Service.id.in_(ids))).all()
    if len(rows) != len(set(ids)) or any((not s.shown) or suspended_on(s, d) for s in rows):
        raise bad_request("One of the chosen services isn't available. Please choose again.", code="service_unavailable")
    order = {sid: i for i, sid in enumerate(ids)}
    return sorted(rows, key=lambda s: order[s.id])


def build_day(db: Session, biz: Business, services: list[Service], d: date, now: datetime,
              exclude_booking_id: int | None = None) -> DayInput:
    interval, duration = interval_and_duration(services, biz.default_interval)
    staff_rows = db.scalars(select(Staff).where(Staff.business_id == biz.id).order_by(Staff.id)).all()
    leaves = set(db.scalars(select(StaffLeave.staff_id).where(
        StaffLeave.business_id == biz.id, StaffLeave.leave_date == d)).all())
    q = select(Booking).where(Booking.business_id == biz.id, Booking.local_date == d, _blocking_filter(now))
    if exclude_booking_id:
        q = q.where(Booking.id != exclude_booking_id)
    busy_by_staff: dict[int | None, list[tuple[int, int]]] = defaultdict(list)
    for b in db.scalars(q).all():
        s_loc, e_loc = to_local(b.start_at), to_local(b.end_at)
        busy_by_staff[b.staff_id].append((s_loc.hour * 60 + s_loc.minute,
                                          (e_loc.hour * 60 + e_loc.minute) if e_loc.date() == d else 24 * 60))
    staff_days = []
    for st in staff_rows:
        works = WEEKDAYS[d.weekday()] in (st.working_days or [])
        staff_days.append(StaffDay(
            id=st.id, name=st.name, start=to_minutes(st.start_time), end=to_minutes(st.end_time),
            break_start=to_min(st.break_start), break_end=to_min(st.break_end),
            available_today=st.shown and works and st.id not in leaves and not suspended_on(st, d),
            busy=busy_by_staff.get(st.id, []),
        ))
    earliest = None
    lnow = local_now(now)
    if d == lnow.date():
        earliest = lnow.hour * 60 + lnow.minute
    return DayInput(
        open_min=to_minutes(biz.open_time), close_min=to_minutes(biz.close_time),
        interval=interval, duration=duration,
        staff=staff_days if staff_rows else [],
        unstaffed_busy=busy_by_staff.get(None, []),
        earliest_start=earliest,
    )


def closures_for(db: Session, business_id: int) -> list[Closure]:
    return db.scalars(select(Closure).where(Closure.business_id == business_id)).all()


def check_date(db: Session, biz: Business, d: date, now: datetime) -> list:
    lo, hi = window(now)
    if d < lo or d > hi:
        raise bad_request("Choose a date within the next 2 months.", code="outside_window")
    closures = closures_for(db, biz.id)
    if not is_open_day(biz, closures, d):
        raise bad_request("This business is closed on the selected date. Please choose another day.", code="closed")
    return closures


# ------------------------------------------------------------------ availability
def availability(db: Session, biz: Business, service_ids: list[int], staff_choice: str, d: date, now: datetime) -> dict:
    expire_holds(db, biz.id, now)
    lo, hi = window(now)
    closures = closures_for(db, biz.id)
    if d < lo or d > hi:
        return {"date": d.isoformat(), "closed": False, "outside_window": True, "slots": []}
    if not is_open_day(biz, closures, d):
        return {"date": d.isoformat(), "closed": True, "slots": []}
    services = bookable_services(db, biz, service_ids, d)
    _validate_staff_choice(db, biz, staff_choice)
    day = build_day(db, biz, services, d, now)
    slots = compute_slots(day, staff_choice if staff_choice == "any" else int(staff_choice))
    return {"date": d.isoformat(), "closed": False, "slots": [
        {"time": s["time"], "label": s["label"], "period": s["period"]} for s in slots]}


def _validate_staff_choice(db: Session, biz: Business, staff_choice: str) -> None:
    if staff_choice == "any":
        return
    try:
        sid = int(staff_choice)
    except (TypeError, ValueError):
        raise bad_request("Choose a valid staff member.")
    st = db.get(Staff, sid)
    if st is None or st.business_id != biz.id or not st.shown:
        raise bad_request("Choose a valid staff member.")


def closed_dates(db: Session, biz: Business, now: datetime) -> list[str]:
    lo, hi = window(now)
    closures = closures_for(db, biz.id)
    out, d = [], lo
    while d <= hi:
        if not is_open_day(biz, closures, d):
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


# ------------------------------------------------------------------ create
@dataclass
class NewBooking:
    service_ids: list[int]
    staff: str
    date: date
    time: str
    name: str
    email: str
    phone: str
    note: str = ""
    source: str = "online"
    status: str | None = None          # manual bookings may choose


def _new_code(db: Session) -> str:
    for _ in range(10):
        code = "CN-" + "".join(secrets.choice(_CODE_CHARS) for _ in range(6))
        if db.scalars(select(Booking.id).where(Booking.code == code)).first() is None:
            return code
    raise AppError(500, "code_gen", "Please try again.")


def _upsert_customer(db: Session, biz: Business, name: str, email: str, phone: str) -> Customer:
    c = db.scalars(select(Customer).where(Customer.business_id == biz.id, Customer.email == email)).first()
    if c is None:
        c = Customer(business_id=biz.id, name=name, email=email, phone=phone)
        db.add(c)
        db.flush()
    else:
        c.name, c.phone = name, phone
    return c


def _check_booking_rate(db: Session, biz: Business, email: str, phone: str, now: datetime) -> None:
    recent = db.scalars(
        select(Booking.id).join(Customer, Customer.id == Booking.customer_id).where(
            Booking.business_id == biz.id, Booking.created_at > now - timedelta(hours=1),
            or_(Customer.email == email, Customer.phone == phone),
        )
    ).all()
    if len(recent) >= 5:
        raise too_many("Too many bookings from these details in the last hour. Please contact the business directly.")


def _clean_contact(nb: NewBooking) -> tuple[str, str, str]:
    name = (nb.name or "").strip()[:120]
    if not name:
        raise bad_request("Enter your name.")
    email = validate_email(nb.email)
    phone = (nb.phone or "").strip()
    digits = "".join(ch for ch in phone if ch.isdigit())
    if len(digits) < 10 or len(digits) > 13:
        raise bad_request("Enter a valid phone number.")
    return name, email, phone[:32]


def create_booking(db: Session, biz: Business, nb: NewBooking, now: datetime) -> tuple[Booking, str]:
    """Returns (booking, plain manage token). Caller commits."""
    if biz.status != "active":
        raise not_found("This booking page isn't available.")
    name, email, phone = _clean_contact(nb)
    try:
        start_min = to_minutes(nb.time)
    except (ValueError, AttributeError):
        raise bad_request("Choose a valid time.")
    with _business_lock(biz.id):
        _lock_business_row(db, biz.id)
        expire_holds(db, biz.id, now)
        if nb.source == "online":
            _check_booking_rate(db, biz, email, phone, now)
            check_date(db, biz, nb.date, now)
        services = bookable_services(db, biz, nb.service_ids, nb.date)
        _validate_staff_choice(db, biz, nb.staff)
        day = build_day(db, biz, services, nb.date, now)
        staff_choice = nb.staff if nb.staff == "any" else int(nb.staff)
        if nb.source == "online":
            free = {s["time"] for s in compute_slots(day, staff_choice)}
            if nb.time not in free:
                raise conflict("That time was just taken or isn't available. Please choose another slot.",
                               code="slot_taken")
            staff_id = pick_staff(day, start_min, staff_choice)
        else:
            # Owner's manual booking: assign a free staff member if possible, otherwise keep the choice.
            staff_id = pick_staff(day, start_min, staff_choice) if day.staff else None
            if staff_id == -1:
                staff_id = None if staff_choice == "any" else int(staff_choice)
        if staff_id == -1:
            raise conflict("That time was just taken. Please choose another slot.", code="slot_taken")
        total = sum(s.price_paise for s in services)
        duration = day.duration
        start_utc = local_to_utc(nb.date, nb.time)
        end_utc = start_utc + timedelta(minutes=duration)
        customer = _upsert_customer(db, biz, name, email, phone)
        deposit = math.ceil(total * biz.deposit_percent / 100) if (biz.deposits_enabled and total > 0) else 0
        if nb.source == "manual":
            status = nb.status or "confirmed"
            if status not in ("confirmed", "requested", "awaiting_payment"):
                status = "confirmed"
            if status != "awaiting_payment":
                deposit = 0
        else:
            status = "awaiting_payment" if deposit else "confirmed"
        b = Booking(
            business_id=biz.id, code=_new_code(db), customer_id=customer.id, staff_id=staff_id,
            start_at=start_utc, end_at=end_utc, local_date=nb.date, status=status,
            total_paise=total, deposit_paise=deposit,
            deposit_status="pending" if deposit else "not_required",
            hold_expires_at=(now + timedelta(minutes=get_settings().booking_hold_minutes)) if status == "awaiting_payment" else None,
            refund_window_hours=biz.refund_window_hours, note=(nb.note or "")[:500], source=nb.source,
        )
        b.items = [BookingItem(service_id=s.id, name=s.name, price_paise=s.price_paise, duration_min=s.duration_min)
                   for s in services]
        token = manage_token(b)
        db.add(b)
        db.flush()
    log.info("booking created business=%s booking=%s status=%s customer=%s", biz.id, b.code, status, mask_email(email))
    return b, token


def manage_token(b: Booking) -> str:
    """
    The booking's secret link token. It is derived from a per-booking random
    nonce, so the same link can be re-sent (confirmation, reminder, recovery)
    without invalidating the one the customer already has. Only its hash is
    stored for lookup. It stops working at the appointment start time.
    """
    if not b.manage_nonce:
        b.manage_nonce = secrets.token_hex(16)
    raw = hmac.new(get_settings().secret_key.encode(), f"manage|{b.code}|{b.manage_nonce}".encode(),
                   hashlib.sha256).digest()
    token = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    b.manage_token_hash = token_hash(token)
    b.manage_token_expires_at = b.start_at
    return token


def booking_by_token(db: Session, token: str, now: datetime) -> Booking:
    if not token:
        raise not_found("This booking link isn't valid.")
    b = db.scalars(select(Booking).where(Booking.manage_token_hash == token_hash(token))).first()
    if b is None:
        raise not_found("This booking link isn't valid.")
    if b.manage_token_expires_at is None or b.manage_token_expires_at <= now:
        raise AppError(410, "link_expired", "This booking link has expired because the appointment time has passed.")
    return b


# ------------------------------------------------------------------ deposits
def deposit_order(db: Session, biz: Business, b: Booking, now: datetime) -> dict:
    """Creates (or reuses) a Razorpay order for an awaiting-payment booking."""
    if b.status != "awaiting_payment" or not b.deposit_paise:
        raise bad_request("This booking doesn't need a payment.")
    if b.hold_expires_at and b.hold_expires_at <= now:
        b.status, b.deposit_status = "expired", "expired"
        db.flush()
        raise AppError(410, "hold_expired", "The slot hold expired. Please book again.")
    existing = db.scalars(select(Payment).where(Payment.booking_id == b.id, Payment.status == "created")).first()
    if existing:
        order_id = existing.order_id
    else:
        hold_until = int((b.start_at - datetime(1970, 1, 1)).total_seconds())
        order_id = gateway.create_order(
            b.deposit_paise, receipt=b.code, notes={"kind": "deposit", "booking": b.code, "business": str(biz.id)},
            transfers=gateway.route_transfers(biz.linked_account_id, b.deposit_paise, hold_until),
        )
        db.add(Payment(business_id=biz.id, kind="deposit", booking_id=b.id, order_id=order_id,
                       amount_paise=b.deposit_paise,
                       payout_status="route_held" if gateway.route_transfers(biz.linked_account_id, 1, 0) else "manual_pending"))
        db.flush()
    return {"order_id": order_id, "amount": b.deposit_paise, "currency": "INR", "key": gateway.public_key(),
            "name": biz.name, "description": f"Deposit for booking {b.code}", "mock": gateway.is_mock(),
            "hold_expires_at": b.hold_expires_at.isoformat() + "Z" if b.hold_expires_at else None}


def confirm_deposit(db: Session, payment: Payment, now: datetime) -> str:
    """Called once a deposit payment is verified. Returns the resulting booking status."""
    b = db.get(Booking, payment.booking_id)
    biz = db.get(Business, payment.business_id)
    if b is None or biz is None:
        return "missing"
    if b.status == "awaiting_payment" and b.hold_expires_at and b.hold_expires_at > now:
        b.status, b.deposit_status, b.hold_expires_at = "confirmed", "paid", None
        _after_confirm(db, biz, b)
        return "confirmed"
    if b.status in ("awaiting_payment", "expired"):
        # Paid after the hold ran out: keep it only if the slot is still free.
        with _business_lock(biz.id):
            _lock_business_row(db, biz.id)
            services = [db.get(Service, i.service_id) for i in b.items if i.service_id]
            still_free = False
            if services and all(services):
                day = build_day(db, biz, services, b.local_date, now, exclude_booking_id=b.id)
                lstart = to_local(b.start_at)
                hhmm = lstart.strftime("%H:%M")
                choice = b.staff_id if b.staff_id else "any"
                still_free = hhmm in {s["time"] for s in compute_slots(day, choice)}
            if still_free and b.start_at > now:
                b.status, b.deposit_status, b.hold_expires_at = "confirmed", "paid", None
                _after_confirm(db, biz, b)
                return "confirmed"
        payment.refund_id = gateway.refund(payment.payment_id, payment.amount_paise,
                                           reverse_transfers=payment.payout_status == "route_held")
        payment.status = "refunded"
        b.status, b.deposit_status = "expired", "refunded"
        _email_customer(db, biz, b, "Your booking couldn't be held",
                        "Your payment arrived after the slot hold ended and the time was taken. "
                        "Your deposit has been refunded in full.")
        return "refunded"
    return b.status


def _after_confirm(db: Session, biz: Business, b: Booking) -> None:
    from services.notifications.service import booking_line, notify

    customer = db.get(Customer, b.customer_id)
    notify(db, biz, "Booking", "New booking", booking_line(b, customer.name, b.items), email_owner=True)
    send_confirmation(db, biz, b, None)


def send_confirmation(db: Session, biz: Business, b: Booking, token: str | None) -> None:
    s = get_settings()
    if token is None:
        token = manage_token(b)
    link = f"{s.base_url}/manage?token={token}"
    lines = [
        f"Your booking at {biz.name} is confirmed.", "",
        f"Booking code: {b.code}",
        f"When: {fmt_local(b.start_at)}",
        f"Services: {', '.join(i.name for i in b.items)}",
        f"Address: {biz.address}, {biz.city}",
        f"Business phone: {biz.phone}",
    ]
    if b.deposit_paise:
        lines.append(f"Deposit paid: ₹{b.deposit_paise / 100:,.2f}")
        lines.append(f"Cancel at least {b.refund_window_hours} hours before for a full deposit refund.")
    lines += ["", f"View, reschedule or cancel: {link}", "This link works until your appointment time."]
    _email_customer(db, biz, b, f"Booking confirmed: {biz.name}", "\n".join(lines))


def _email_customer(db: Session, biz: Business, b: Booking, subject: str, body: str) -> None:
    from services.auth.email_sender import send_email

    c = db.get(Customer, b.customer_id)
    if c:
        send_email(c.email, subject, body, critical=False)


# ------------------------------------------------------------------ cancel / reschedule
def _refund_deposit(db: Session, b: Booking) -> bool:
    p = db.scalars(select(Payment).where(Payment.booking_id == b.id, Payment.status == "paid")).first()
    if p is None:
        return False
    p.refund_id = gateway.refund(p.payment_id, p.amount_paise, reverse_transfers=p.payout_status == "route_held")
    p.status = "refunded"
    return True


def cancel_booking(db: Session, biz: Business, b: Booking, by: str, now: datetime) -> dict:
    if b.status not in ("awaiting_payment", "requested", "confirmed"):
        raise bad_request("This booking can't be cancelled any more.")
    refunded = forfeited = False
    if b.deposit_status == "paid":
        hours_left = (b.start_at - now) / timedelta(hours=1)
        if by == "owner" or hours_left >= b.refund_window_hours:
            refunded = _refund_deposit(db, b)
        else:
            forfeited = True
    if refunded:
        b.status, b.deposit_status = "refunded", "refunded"
    elif forfeited:
        b.status, b.deposit_status = "deposit_forfeited", "forfeited"
    else:
        b.status = "cancelled"
        if b.deposit_status == "pending":
            b.deposit_status = "expired"
    b.cancelled_at = now
    b.hold_expires_at = None
    from services.notifications.service import booking_line, notify

    customer = db.get(Customer, b.customer_id)
    who = "Customer" if by == "customer" else "You"
    notify(db, biz, "Cancellation", "Booking cancelled", f"{who} cancelled · " + booking_line(b, customer.name, b.items),
           email_owner=(by == "customer"))
    note = ("Your deposit will be refunded in full." if refunded else
            "Because this was within the cancellation window, the deposit is not refunded." if forfeited else "")
    _email_customer(db, biz, b, f"Booking cancelled: {biz.name}",
                    f"Your booking {b.code} for {fmt_local(b.start_at)} has been cancelled. {note}".strip())
    db.flush()
    return {"status": b.status, "refunded": refunded, "forfeited": forfeited}


def reschedule_booking(db: Session, biz: Business, b: Booking, new_date: date, new_time: str, now: datetime,
                       *, by: str) -> Booking:
    if b.status not in ("requested", "confirmed"):
        raise bad_request("Only confirmed or requested bookings can be rescheduled.")
    try:
        start_min = to_minutes(new_time)
    except (ValueError, AttributeError):
        raise bad_request("Choose a valid time.")
    with _business_lock(biz.id):
        _lock_business_row(db, biz.id)
        expire_holds(db, biz.id, now)
        if by == "customer":
            check_date(db, biz, new_date, now)
        services = [db.get(Service, i.service_id) for i in b.items if i.service_id]
        if not services or not all(services):
            raise bad_request("A service on this booking no longer exists. Please contact the business.")
        day = build_day(db, biz, services, new_date, now, exclude_booking_id=b.id)
        choice = b.staff_id if b.staff_id else "any"
        # Both customers and owners can only move a booking to a genuinely free slot
        # (staff hours, breaks, leave and other bookings are all checked).
        if by == "owner":
            closures = closures_for(db, biz.id)
            if not is_open_day(biz, closures, new_date):
                raise bad_request("The business is closed on that date.", code="closed")
        free = {s["time"] for s in compute_slots(day, choice)}
        if new_time not in free:
            raise conflict("That time isn't available: it clashes with another booking, a break, "
                           "or falls outside working hours.", code="slot_taken")
        if b.staff_id is None and day.staff:
            picked = pick_staff(day, start_min, "any")
            b.staff_id = picked if picked != -1 else None
        if not (0 <= start_min < 24 * 60):
            raise bad_request("Choose a valid time.")
        duration = sum(i.duration_min for i in b.items) or day.duration
        b.start_at = local_to_utc(new_date, new_time)
        b.end_at = b.start_at + timedelta(minutes=duration)
        b.local_date = new_date
        b.manage_token_expires_at = b.start_at
        b.reminder_sent = False
        db.flush()
    from services.notifications.service import booking_line, notify

    customer = db.get(Customer, b.customer_id)
    notify(db, biz, "Booking", "Booking rescheduled", booking_line(b, customer.name, b.items),
           email_owner=(by == "customer"))
    _email_customer(db, biz, b, f"Booking rescheduled: {biz.name}",
                    f"Your booking {b.code} is now on {fmt_local(b.start_at)}.")
    return b


def booking_public_view(db: Session, biz: Business, b: Booking) -> dict:
    staff = db.get(Staff, b.staff_id) if b.staff_id else None
    c = db.get(Customer, b.customer_id)
    loc = to_local(b.start_at)
    return {
        "code": b.code, "status": b.status, "status_label": STATUS_LABELS.get(b.status, b.status),
        "date": loc.date().isoformat(), "time": loc.strftime("%H:%M"),
        "services": [{"name": i.name, "price": i.price_paise / 100, "minutes": i.duration_min} for i in b.items],
        "staff": staff.name if staff else "Any available",
        "total": b.total_paise / 100, "deposit": b.deposit_paise / 100,
        "deposit_status": b.deposit_status, "refund_window_hours": b.refund_window_hours,
        "customer": {"name": c.name, "email": c.email, "phone": c.phone} if c else None,
        "note": b.note,
        "hold_expires_at": b.hold_expires_at.isoformat() + "Z" if b.hold_expires_at else None,
    }
