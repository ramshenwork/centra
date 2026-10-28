"""
services/booking/status_change.py
---------------------------------
Owner-initiated status changes from the dashboard: the "Change status" popup
and the quick Confirm / Complete / No-show / Cancel buttons both land here
(through POST /api/dashboard/appointments/{id}/status).

This file decides which status may change to which, and what each change does
to the deposit. The route only loads the booking (tenant-checked with owned())
and commits; it holds no rules.

Rules
  * The owner can set: confirmed, completed, no_show, cancelled, rejected
    (rejected is shown as "Denied" in the popup).
  * Any of those can be changed to any other, so a wrong tap can be undone.
    The one exception: an unpaid hold (awaiting_payment) can only be
    confirmed, cancelled or denied.
  * refunded, deposit_forfeited and expired are locked: the money has already
    moved (or the hold lapsed), so they cannot be changed.

Deposit effects
  * -> no_show        a paid deposit becomes forfeited (as before).
  * no_show -> confirmed / completed   a deposit forfeited by that no-show is
                      restored to paid.
  * -> cancelled / rejected from a live booking: the normal cancel rules run
                      (the customer is emailed; an owner cancel refunds a paid
                      deposit). From completed / no_show the booking is first
                      treated as confirmed again, so the same rules apply.
  * cancelled <-> rejected   only the label changes (nothing is emailed or
                      refunded a second time).
  * Moving out of cancelled / rejected clears cancelled_at; a deposit that was
    never collected (pending / expired) becomes not_required.

Slots
  Completed, no-show, cancelled and rejected bookings no longer block their
  time. Bringing one of them back to CONFIRMED (while it is still upcoming)
  re-checks that nobody else has taken the same staff member's time.

No customer email is sent when a booking is brought back; the dashboard tells
the owner so.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app_platform.errors import bad_request, conflict
from dbhandlers.models import Booking, Business
from services.booking import engine

SETTABLE = ("confirmed", "completed", "no_show", "cancelled", "rejected")
LOCKED = ("refunded", "deposit_forfeited", "expired")
_LOCKED_REASON = {
    "refunded": "its deposit has already been refunded",
    "deposit_forfeited": "its deposit has already been forfeited",
    "expired": "it expired without payment",
}
_FROM_AWAITING_PAYMENT = ("confirmed", "cancelled", "rejected")
_NOT_NOTIFIED = "The customer isn't notified automatically."


def _label(status: str) -> str:
    return engine.STATUS_LABELS.get(status, status)


def allowed_targets(current: str) -> tuple[str, ...]:
    """Statuses the owner may change a booking in `current` to."""
    if current in LOCKED or current not in engine.STATUS_LABELS:
        return ()
    if current == "awaiting_payment":
        return _FROM_AWAITING_PAYMENT
    return tuple(t for t in SETTABLE if t != current)


def _clashes(db: Session, b: Booking, now: datetime) -> bool:
    """True if another live booking overlaps this one for the same staff member
    (or, for an unassigned booking, another unassigned one: the one-chair rule)."""
    q = select(Booking.id).where(
        Booking.business_id == b.business_id, Booking.id != b.id,
        Booking.start_at < b.end_at, Booking.end_at > b.start_at,
        engine._blocking_filter(now),
    )
    q = q.where(Booking.staff_id == b.staff_id) if b.staff_id is not None else q.where(Booking.staff_id.is_(None))
    return db.scalar(q.limit(1)) is not None


def _cancel_or_deny(db: Session, biz: Business, b: Booking, target: str, now: datetime) -> None:
    cur = b.status
    if cur in ("cancelled", "rejected"):
        b.status = target                    # label only: nothing to refund or email again
        return
    if cur in ("completed", "no_show"):
        if cur == "no_show" and b.deposit_status == "forfeited":
            b.deposit_status = "paid"        # undo the no-show forfeit, then apply the cancel rules
        b.status = "confirmed"
    result = engine.cancel_booking(db, biz, b, "owner", now)
    if target == "rejected" and result["status"] == "cancelled":
        b.status = "rejected"


def _set_live_status(db: Session, b: Booking, target: str, now: datetime) -> str:
    cur = b.status
    reinstating = cur in ("cancelled", "rejected")
    if target == "confirmed" and cur in ("cancelled", "rejected", "completed", "no_show") and b.end_at > now:
        if _clashes(db, b, now):
            raise conflict("That time is now taken by another booking, so this one can't be confirmed again. "
                           "Create a new booking for another time instead.", code="slot_taken")
    if reinstating:
        b.cancelled_at = None
    if (reinstating or target == "confirmed") and b.deposit_status in ("pending", "expired"):
        b.deposit_status = "not_required"    # a deposit that was never collected
    if cur == "no_show" and target in ("confirmed", "completed") and b.deposit_status == "forfeited":
        b.deposit_status = "paid"
    if target == "no_show" and b.deposit_status == "paid":
        b.deposit_status = "forfeited"
    if reinstating or target == "confirmed":
        b.hold_expires_at = None
    b.status = target
    return _NOT_NOTIFIED if (target == "confirmed" and cur in ("cancelled", "rejected")) else ""


def change_status(db: Session, biz: Business, b: Booking, target: str, now: datetime) -> dict:
    if target not in SETTABLE:
        raise bad_request("Choose a valid status.")
    with engine._business_lock(biz.id):
        engine._lock_business_row(db, biz.id)
        db.refresh(b)                        # another tab may have changed it a moment ago
        cur = b.status
        if cur in LOCKED:
            raise bad_request(f"This booking can't be changed because {_LOCKED_REASON[cur]}.", code="status_locked")
        if target == cur:
            raise bad_request(f"This booking is already {_label(cur)}.")
        if target not in allowed_targets(cur):
            raise bad_request(f"A {_label(cur).lower()} booking can't be changed to {_label(target).lower()}.")
        note = ""
        if target in ("cancelled", "rejected"):
            _cancel_or_deny(db, biz, b, target, now)
        else:
            note = _set_live_status(db, b, target, now)
        db.flush()
    return {"status": _label(b.status), "note": note}
