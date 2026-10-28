"""
services/business/customer_lookup.py
------------------------------------
Read-only customer lookup for the owner dashboard.

  * Customers are grouped by PHONE NUMBER (last 10 digits), because the same
    person can exist as several customer rows (a different email, or a
    walk-in plus an online booking). Rows with no usable phone fall back to
    their email.
  * The lookup returns, per customer: how many times they booked, how many in
    each status, and their last booking date. Counts cover their whole
    history; the optional date range only decides WHICH customers are listed
    (anyone with at least one appointment inside the range).
  * history_for() returns one customer's full booking list.

Everything is scoped to the signed-in owner's business (business_id filter in
every query). Nothing here writes to the database.

The grouping and filtering is plain Python over simple dicts (group_customers),
so it can be tested without a database.
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app_platform.errors import bad_request
from app_platform.timeutil import to_local
from dbhandlers.models import Booking, BookingItem, Customer, Staff
from services.booking.engine import STATUS_LABELS

MAX_RESULTS = 100
MAX_HISTORY = 200
_KEY_RE = re.compile(r"^(?:\d{7,15}|e:.{1,254})$")


def customer_key(phone: str | None, email: str | None) -> str:
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) >= 7:
        return digits[-10:]
    return "e:" + (email or "").strip().lower()


def parse_day(value: str, label: str) -> date | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise bad_request(f"Enter a valid {label} date.")


def valid_key(key: str) -> str:
    key = (key or "").strip()
    if not _KEY_RE.match(key):
        raise bad_request("Choose a customer from the list.")
    return key


def group_customers(rows: list[dict], q: str = "", d_from: date | None = None, d_to: date | None = None,
                    limit: int = MAX_RESULTS) -> dict:
    """rows: dicts with id, code, date (date), status, name, phone, email."""
    q = (q or "").strip().lower()[:80]
    q_digits = re.sub(r"\D", "", q)
    groups: dict[str, dict] = {}
    for r in sorted(rows, key=lambda x: (x["date"], x["id"])):          # oldest first, so the last row wins display fields
        g = groups.setdefault(customer_key(r["phone"], r["email"]), {
            "names": [], "phones": set(), "emails": set(), "total": 0, "statuses": Counter(), "last": None,
            "in_range": False, "name": "", "phone": "", "email": ""})
        g["total"] += 1
        g["statuses"][STATUS_LABELS.get(r["status"], r["status"])] += 1
        g["last"] = r["date"]
        if r["name"] and r["name"] not in g["names"]:
            g["names"].append(r["name"])
        g["phones"].add(r["phone"] or "")
        g["emails"].add((r["email"] or "").lower())
        g["name"], g["phone"], g["email"] = r["name"] or g["name"], r["phone"] or g["phone"], r["email"] or g["email"]
        if (d_from is None or r["date"] >= d_from) and (d_to is None or r["date"] <= d_to):
            g["in_range"] = True
    out = []
    for key, g in groups.items():
        if (d_from or d_to) and not g["in_range"]:
            continue
        if q:
            text = " ".join(g["names"] + sorted(g["emails"])).lower()
            digits_hit = len(q_digits) >= 3 and any(q_digits in re.sub(r"\D", "", p) for p in g["phones"])
            if q not in text and not digits_hit and q not in " ".join(sorted(g["phones"])).lower():
                continue
        email = g["email"]
        out.append({
            "key": key, "name": g["name"], "names": g["names"], "phone": g["phone"],
            "email": "" if email.endswith("@no-email.centra") else email,
            "total": g["total"], "statuses": dict(g["statuses"]), "last": g["last"].isoformat() if g["last"] else "",
        })
    out.sort(key=lambda c: (c["last"], c["name"].lower()), reverse=True)
    return {"customers": out[:limit], "matched": len(out), "truncated": len(out) > limit}


def _load_rows(db: Session, business_id: int) -> list[dict]:
    q = (select(Booking.id, Booking.code, Booking.local_date, Booking.status, Customer.name, Customer.phone, Customer.email)
         .join(Customer, Customer.id == Booking.customer_id)
         .where(Booking.business_id == business_id, Customer.business_id == business_id))
    return [{"id": i, "code": c, "date": d, "status": s, "name": n, "phone": p, "email": e}
            for i, c, d, s, n, p, e in db.execute(q).all()]


def lookup(db: Session, business_id: int, q: str, date_from: str, date_to: str) -> dict:
    d_from, d_to = parse_day(date_from, "start"), parse_day(date_to, "end")
    if d_from and d_to and d_to < d_from:
        raise bad_request("The end date can't be before the start date.")
    return group_customers(_load_rows(db, business_id), q, d_from, d_to)


def history_for(db: Session, business_id: int, key: str) -> dict:
    key = valid_key(key)
    rows = [r for r in _load_rows(db, business_id) if customer_key(r["phone"], r["email"]) == key]
    rows.sort(key=lambda r: (r["date"], r["id"]), reverse=True)
    rows = rows[:MAX_HISTORY]
    ids = [r["id"] for r in rows]
    items: dict[int, list[str]] = {}
    staff_by_booking: dict[int, str] = {}
    times: dict[int, str] = {}
    if ids:
        for bid, name in db.execute(select(BookingItem.booking_id, BookingItem.name).where(BookingItem.booking_id.in_(ids))).all():
            items.setdefault(bid, []).append(name)
        for b, sname in db.execute(select(Booking, Staff.name).outerjoin(Staff, Staff.id == Booking.staff_id)
                                   .where(Booking.business_id == business_id, Booking.id.in_(ids))).all():
            staff_by_booking[b.id] = sname or "Unassigned"
            times[b.id] = to_local(b.start_at).strftime("%H:%M")
    return {"bookings": [{
        "id": r["id"], "code": r["code"], "date": r["date"].isoformat(), "time": times.get(r["id"], ""),
        "services": ", ".join(items.get(r["id"], [])), "staff": staff_by_booking.get(r["id"], ""),
        "status": STATUS_LABELS.get(r["status"], r["status"]), "name": r["name"],
    } for r in rows]}
