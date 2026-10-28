"""
services/business/router.py
---------------------------
Everything the owner does in the management dashboard. Every handler
gets its business from owner_ctx (the signed-in owner's membership) and
uses owned() for any id coming from the browser, so one business can
never read or change another's data.
"""
from __future__ import annotations

import asyncio
import json
from datetime import date, timedelta

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app_platform.errors import bad_request, not_found
from app_platform.timeutil import WEEKDAY_NAMES, WEEKDAYS, is_valid_hhmm, to_minutes
from dbhandlers.database import get_db, session_factory, utcnow
from dbhandlers.models import (
    Booking, Closure, Customer, Notification, Service, Staff, StaffLeave,
)
from services.auth.sessions import current_user
from services.booking import engine, status_change
from services.business.access import Ctx, business_for_user, owned, owner_ctx
from services.business.areas import areas_limit, clean_areas
from services.business import customer_lookup
from services.business.closures import festivals
from services.business.state import build_state
from services.notifications.service import notify
from services.payments import pricing
from services.payments.service import create_subscription_order
from services.signup.service import check_slug
from services.storage.logos import save_logo

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


# ------------------------------------------------------------------ schemas
class ProfileIn(BaseModel):
    name: str
    city: str
    address: str = ""
    phone: str = ""
    whatsapp: str | None = None
    slug: str
    open: str
    close: str
    days: list[str]
    areas: list[str] | None = None


class SettingsIn(BaseModel):
    showPricing: bool | None = None
    allowMultiple: bool | None = None
    deposits: bool | None = None
    depositPercent: int | None = None
    refundWindow: int | None = None
    sound: bool | None = None


class ServiceIn(BaseModel):
    name: str
    price: float = 0
    duration: int = 30
    interval: int = 30
    shown: bool | None = None


class StaffIn(BaseModel):
    name: str
    role: str = ""
    days: list[str] | None = None
    start: str
    end: str
    bs: str = ""
    be: str = ""
    interval: int | None = None
    shown: bool | None = None


class SuspendIn(BaseModel):
    period: str  # number of days or "forever"


class LeaveIn(BaseModel):
    date: date
    reason: str = ""


class ClosureIn(BaseModel):
    type: str
    label: str
    note: str = ""
    start: date | None = None
    end: date | None = None
    weekday: str | None = None
    day: int | None = None
    month: int | None = None
    festival: str | None = None


class ManualIn(BaseModel):
    customer: str
    phone: str
    email: str = ""
    service_ids: list[int]
    staff: str = "any"
    date: date
    time: str
    status: str = "confirmed"
    note: str = ""
    override: bool = False


class StatusIn(BaseModel):
    status: str


class RescheduleIn(BaseModel):
    date: date
    time: str


class NotesIn(BaseModel):
    notes: str


class ReadIn(BaseModel):
    id: int | None = None
    all: bool = False


class RenewIn(BaseModel):
    plan: str | None = None
    billing: str | None = None


# ------------------------------------------------------------------ state
@router.get("/state")
def state(ctx: Ctx = Depends(owner_ctx)):
    return build_state(ctx.db, ctx.business, utcnow())


# ------------------------------------------------------------------ profile & settings
@router.put("/profile")
def save_profile(body: ProfileIn, ctx: Ctx = Depends(owner_ctx)):
    biz = ctx.business
    name, city = body.name.strip(), body.city.strip()
    if not name or not city:
        raise bad_request("Business name and city are required.")
    if not body.address.strip():
        raise bad_request("Enter your street address.")
    if not (is_valid_hhmm(body.open) and is_valid_hhmm(body.close)) or to_minutes(body.open) >= to_minutes(body.close):
        raise bad_request("Closing time must be later than opening time.")
    days = [d for d in body.days if d in WEEKDAYS]
    if not days:
        raise bad_request("Choose at least one working day.")
    biz.slug = check_slug(ctx.db, body.slug, biz.id)
    if body.areas is not None:
        biz.areas = clean_areas(body.areas)
    biz.name, biz.city, biz.address, biz.phone = name[:120], city[:80], body.address.strip()[:255], body.phone.strip()[:32]
    if body.whatsapp is not None:
        biz.whatsapp = body.whatsapp.strip()[:32]
    biz.open_time, biz.close_time = body.open, body.close
    biz.working_days = sorted(set(days), key=WEEKDAYS.index)
    ctx.db.commit()
    return {"ok": True}


@router.post("/logo")
async def logo(file: UploadFile = File(...), ctx: Ctx = Depends(owner_ctx)):
    data = await file.read(1_000_001)
    ctx.business.logo_url = save_logo(ctx.business.id, data)
    ctx.db.commit()
    return {"ok": True, "logo_url": ctx.business.logo_url}


@router.put("/settings")
def save_settings(body: SettingsIn, ctx: Ctx = Depends(owner_ctx)):
    biz = ctx.business
    if body.showPricing is not None:
        biz.show_pricing = body.showPricing
    if body.allowMultiple is not None:
        biz.allow_multiple = body.allowMultiple
    if body.deposits is not None:
        biz.deposits_enabled = body.deposits
    if body.depositPercent is not None:
        if not 1 <= body.depositPercent <= 100:
            raise bad_request("Deposit percentage must be between 1 and 100.")
        biz.deposit_percent = body.depositPercent
    if body.refundWindow is not None:
        if not 0 <= body.refundWindow <= 720:
            raise bad_request("Choose a valid refund window.")
        biz.refund_window_hours = body.refundWindow
    if body.sound is not None:
        biz.sound_alerts = body.sound
    ctx.db.commit()
    return {"ok": True}


# ------------------------------------------------------------------ services
def _service_values(body: ServiceIn) -> dict:
    name = body.name.strip()
    if not name:
        raise bad_request("Enter a service name.")
    if body.price < 0 or not 5 <= body.duration <= 720 or not 5 <= body.interval <= 480:
        raise bad_request("Check the price, duration (5–720 min) and interval (5–480 min).")
    return {"name": name[:120], "price_paise": int(round(body.price * 100)), "duration_min": body.duration,
            "interval_min": body.interval}


@router.post("/services")
def add_service(body: ServiceIn, ctx: Ctx = Depends(owner_ctx)):
    vals = _service_values(body)
    last = ctx.db.scalars(select(Service.sort).where(Service.business_id == ctx.business.id)
                          .order_by(Service.sort.desc())).first() or 0
    ctx.db.add(Service(business_id=ctx.business.id, sort=last + 1, **vals))
    ctx.db.commit()
    return {"ok": True}


@router.put("/services/{sid}")
def edit_service(sid: int, body: ServiceIn, ctx: Ctx = Depends(owner_ctx)):
    s = owned(ctx.db, Service, sid, ctx.business.id)
    for k, v in _service_values(body).items():
        setattr(s, k, v)
    if body.shown is not None:
        s.shown = body.shown
    ctx.db.commit()
    return {"ok": True}


@router.post("/services/{sid}/toggle")
def toggle_service(sid: int, ctx: Ctx = Depends(owner_ctx)):
    s = owned(ctx.db, Service, sid, ctx.business.id)
    s.shown = not s.shown
    ctx.db.commit()
    return {"ok": True, "shown": s.shown}


def _apply_suspend(obj, period: str, today: date) -> str:
    if period == "forever":
        obj.suspended_forever, obj.suspended_until = True, None
        return "until turned back on"
    try:
        days = int(period)
    except ValueError:
        raise bad_request("Choose a suspension period.")
    if not 1 <= days <= 365:
        raise bad_request("Choose a suspension period.")
    obj.suspended_forever = False
    obj.suspended_until = today + timedelta(days=days - 1)  # N days, starting today
    return f"until {obj.suspended_until.strftime('%d %b %Y')}"


@router.post("/services/{sid}/suspend")
def suspend_service(sid: int, body: SuspendIn, ctx: Ctx = Depends(owner_ctx)):
    s = owned(ctx.db, Service, sid, ctx.business.id)
    until = _apply_suspend(s, body.period, engine.today_local(utcnow()))
    notify(ctx.db, ctx.business, "System", "Service suspended", f"{s.name} is suspended {until}.")
    ctx.db.commit()
    return {"ok": True}


@router.post("/services/{sid}/resume")
def resume_service(sid: int, ctx: Ctx = Depends(owner_ctx)):
    s = owned(ctx.db, Service, sid, ctx.business.id)
    s.suspended_forever, s.suspended_until = False, None
    ctx.db.commit()
    return {"ok": True}


# ------------------------------------------------------------------ staff
def _staff_values(body: StaffIn) -> dict:
    name = body.name.strip()
    if not name:
        raise bad_request("Enter the staff member's name.")
    if not (is_valid_hhmm(body.start) and is_valid_hhmm(body.end)) or to_minutes(body.start) >= to_minutes(body.end):
        raise bad_request("End time must be later than start time.")
    bs, be = body.bs or "", body.be or ""
    if bs or be:
        if not (is_valid_hhmm(bs) and is_valid_hhmm(be)) or to_minutes(bs) >= to_minutes(be):
            raise bad_request("Break end must be later than break start.")
    vals = {"name": name[:120], "role": body.role.strip()[:80], "start_time": body.start, "end_time": body.end,
            "break_start": bs, "break_end": be,
            "interval_min": body.interval if body.interval and 5 <= body.interval <= 480 else None}
    if body.days is not None:
        days = [d for d in body.days if d in WEEKDAYS]
        if not days:
            raise bad_request("Choose at least one working day.")
        vals["working_days"] = sorted(set(days), key=WEEKDAYS.index)
    return vals


@router.post("/staff")
def add_staff(body: StaffIn, ctx: Ctx = Depends(owner_ctx)):
    vals = _staff_values(body)
    vals.setdefault("working_days", list(ctx.business.working_days or WEEKDAYS[:5]))
    ctx.db.add(Staff(business_id=ctx.business.id, **vals))
    ctx.db.commit()
    return {"ok": True}


@router.put("/staff/{sid}")
def edit_staff(sid: int, body: StaffIn, ctx: Ctx = Depends(owner_ctx)):
    st = owned(ctx.db, Staff, sid, ctx.business.id)
    for k, v in _staff_values(body).items():
        setattr(st, k, v)
    if body.shown is not None:
        st.shown = body.shown
    ctx.db.commit()
    return {"ok": True}


@router.post("/staff/{sid}/suspend")
def suspend_staff(sid: int, body: SuspendIn, ctx: Ctx = Depends(owner_ctx)):
    st = owned(ctx.db, Staff, sid, ctx.business.id)
    until = _apply_suspend(st, body.period, engine.today_local(utcnow()))
    notify(ctx.db, ctx.business, "System", "Staff suspended", f"{st.name} is suspended {until}.")
    ctx.db.commit()
    return {"ok": True}


@router.post("/staff/{sid}/resume")
def resume_staff(sid: int, ctx: Ctx = Depends(owner_ctx)):
    st = owned(ctx.db, Staff, sid, ctx.business.id)
    st.suspended_forever, st.suspended_until = False, None
    ctx.db.commit()
    return {"ok": True}


@router.post("/staff/{sid}/leave")
def add_leave(sid: int, body: LeaveIn, ctx: Ctx = Depends(owner_ctx)):
    st = owned(ctx.db, Staff, sid, ctx.business.id)
    exists = ctx.db.scalars(select(StaffLeave).where(StaffLeave.staff_id == st.id,
                                                     StaffLeave.leave_date == body.date)).first()
    if exists is None:
        ctx.db.add(StaffLeave(business_id=ctx.business.id, staff_id=st.id, leave_date=body.date,
                              reason=body.reason.strip()[:120]))
    ctx.db.commit()
    return {"ok": True}


@router.delete("/staff/{sid}/leave/{day}")
def remove_leave(sid: int, day: date, ctx: Ctx = Depends(owner_ctx)):
    st = owned(ctx.db, Staff, sid, ctx.business.id)
    row = ctx.db.scalars(select(StaffLeave).where(StaffLeave.staff_id == st.id, StaffLeave.leave_date == day)).first()
    if row:
        ctx.db.delete(row)
        ctx.db.commit()
    return {"ok": True}


# ------------------------------------------------------------------ closures
def _closure_values(body: ClosureIn) -> dict:
    label = body.label.strip()
    if not label:
        raise bad_request("Enter a label for this closure.")
    vals = {"type": body.type, "label": label[:80], "note": body.note.strip()[:255], "start_date": None,
            "end_date": None, "weekday": None, "day_of_month": None, "month": None, "festival_key": ""}
    if body.type == "range":
        if not body.start or not body.end or body.end < body.start:
            raise bad_request("Choose a valid date range.")
        vals.update(start_date=body.start, end_date=body.end)
    elif body.type == "weekly":
        if body.weekday not in WEEKDAY_NAMES:
            raise bad_request("Choose a weekday.")
        vals["weekday"] = WEEKDAY_NAMES.index(body.weekday)
    elif body.type == "monthly":
        if not body.day or not 1 <= body.day <= 31:
            raise bad_request("Choose a day of the month.")
        vals["day_of_month"] = body.day
    elif body.type == "yearly":
        if not body.month or not 1 <= body.month <= 12 or not body.day or not 1 <= body.day <= 31:
            raise bad_request("Choose a month and day.")
        try:
            date(2024, body.month, body.day)  # leap year accepts 29 Feb
        except ValueError:
            raise bad_request("That date doesn't exist.")
        vals.update(month=body.month, day_of_month=body.day)
    elif body.type == "festival":
        if body.festival not in festivals():
            raise bad_request("Choose a festival.")
        vals["festival_key"] = body.festival
    else:
        raise bad_request("Choose a closure type.")
    return vals


@router.post("/closures")
def add_closure(body: ClosureIn, ctx: Ctx = Depends(owner_ctx)):
    ctx.db.add(Closure(business_id=ctx.business.id, **_closure_values(body)))
    ctx.db.commit()
    return {"ok": True}


@router.put("/closures/{cid}")
def edit_closure(cid: int, body: ClosureIn, ctx: Ctx = Depends(owner_ctx)):
    c = owned(ctx.db, Closure, cid, ctx.business.id)
    for k, v in _closure_values(body).items():
        setattr(c, k, v)
    ctx.db.commit()
    return {"ok": True}


@router.delete("/closures/{cid}")
def delete_closure(cid: int, ctx: Ctx = Depends(owner_ctx)):
    c = owned(ctx.db, Closure, cid, ctx.business.id)
    ctx.db.delete(c)
    ctx.db.commit()
    return {"ok": True}


# ------------------------------------------------------------------ appointments
@router.get("/availability")
def owner_availability(services: str, date: date, staff: str = "any", ctx: Ctx = Depends(owner_ctx)):
    ids = [int(x) for x in services.split(",") if x.strip().isdigit()]
    return engine.availability(ctx.db, ctx.business, ids, staff, date, utcnow())


@router.post("/appointments")
def manual_booking(body: ManualIn, ctx: Ctx = Depends(owner_ctx)):
    email = body.email.strip() or f"walkin-{''.join(c for c in body.phone if c.isdigit())}@no-email.centra"
    nb = engine.NewBooking(service_ids=body.service_ids, staff=body.staff, date=body.date, time=body.time,
                           name=body.customer, email=email, phone=body.phone, note=body.note, source="manual",
                           status=body.status if body.status in ("confirmed", "requested", "awaiting_payment") else "confirmed",
                           override=body.override)
    b, _ = engine.create_booking(ctx.db, ctx.business, nb, utcnow())
    notify(ctx.db, ctx.business, "Booking", "Manual booking created", f"{body.customer} · {b.code}")
    ctx.db.commit()
    return {"ok": True, "code": b.code}


@router.post("/appointments/{bid}/status")
def set_status(bid: int, body: StatusIn, ctx: Ctx = Depends(owner_ctx)):
    b = owned(ctx.db, Booking, bid, ctx.business.id)
    result = status_change.change_status(ctx.db, ctx.business, b, body.status, utcnow())
    ctx.db.commit()
    return {"ok": True, **result}


@router.post("/appointments/{bid}/reschedule")
def reschedule(bid: int, body: RescheduleIn, ctx: Ctx = Depends(owner_ctx)):
    b = owned(ctx.db, Booking, bid, ctx.business.id)
    engine.reschedule_booking(ctx.db, ctx.business, b, body.date, body.time, utcnow(), by="owner")
    if b.status == "requested":
        b.status = "confirmed"
    ctx.db.commit()
    return {"ok": True}


# ------------------------------------------------------------------ customers & notifications
@router.get("/customers/lookup")
def customers_lookup(q: str = "", date_from: str = "", date_to: str = "", ctx: Ctx = Depends(owner_ctx)):
    return customer_lookup.lookup(ctx.db, ctx.business.id, q, date_from, date_to)


@router.get("/customers/history")
def customers_history(key: str = "", ctx: Ctx = Depends(owner_ctx)):
    return customer_lookup.history_for(ctx.db, ctx.business.id, key)


@router.put("/customers/{cid}/notes")
def customer_notes(cid: int, body: NotesIn, ctx: Ctx = Depends(owner_ctx)):
    c = owned(ctx.db, Customer, cid, ctx.business.id)
    c.notes = body.notes[:2000]
    ctx.db.commit()
    return {"ok": True}


@router.post("/notifications/read")
def read_notifications(body: ReadIn, ctx: Ctx = Depends(owner_ctx)):
    if body.all:
        for n in ctx.db.scalars(select(Notification).where(Notification.business_id == ctx.business.id,
                                                           Notification.unread.is_(True))).all():
            n.unread = False
    elif body.id:
        owned(ctx.db, Notification, body.id, ctx.business.id).unread = False
    ctx.db.commit()
    return {"ok": True}


@router.get("/events")
async def events(request: Request, after: int = 0, db: Session = Depends(get_db), user=Depends(current_user)):
    """Server-sent events: pushes new notifications to the open dashboard (the bell)."""
    biz = business_for_user(db, user)
    if biz is None:
        raise not_found()
    biz_id = biz.id
    db.close()

    async def stream():
        last = after
        idle = 0
        while not await request.is_disconnected():
            with session_factory()() as s:
                rows = s.scalars(select(Notification).where(Notification.business_id == biz_id,
                                                            Notification.id > last).order_by(Notification.id)).all()
                for n in rows:
                    last = n.id
                    yield f"event: notification\ndata: {json.dumps({'id': n.id, 'type': n.type, 'title': n.title, 'body': n.body})}\n\n"
            idle += 1
            if idle % 5 == 0:
                yield ": keep-alive\n\n"
            await asyncio.sleep(3)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ------------------------------------------------------------------ subscription
@router.post("/subscription/renew")
def renew(body: RenewIn, ctx: Ctx = Depends(owner_ctx)):
    plan = body.plan or ctx.business.plan
    billing = body.billing or ctx.business.billing
    if plan == "Top" and not ctx.business.meta_verified:
        raise bad_request("Top is only available for Meta-verified businesses.")
    q = pricing.quote(ctx.db, ctx.user.id, plan, billing)
    order = create_subscription_order(ctx.db, ctx.business, ctx.user.id, q, utcnow())
    ctx.db.commit()
    return {"ok": True, "order": order}


@router.get("/areas-limit")
def get_areas_limit(ctx: Ctx = Depends(owner_ctx)):
    return {"limit": areas_limit()}


@router.get("/pricing")
def dashboard_pricing(ctx: Ctx = Depends(owner_ctx)):
    return pricing.public_table(first_time_top=pricing.is_first_time_top(ctx.db, ctx.user.id))
