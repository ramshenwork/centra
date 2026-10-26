"""
services/signup/service.py
--------------------------
Turns the sign-up wizard's answers into database rows.

Order of events (RR's decisions):
1. Owner details + email OTP. Nothing is created until the code is verified.
2. On verification: the User, a draft Business and the Membership are created,
   and the owner is signed in. From here the wizard saves its progress on
   the server, so going back (or coming back later) never loses data.
3. At "Review & pay" the whole wizard is validated again on the server and
   written as ONE unit: business details, services and staff together, or
   nothing. Then a Razorpay order is created.
4. The business stays a draft (booking page not live) until the payment is
   confirmed server-side.

Assumption to confirm with RR: the wizard doesn't ask for service
durations, so each service starts with duration = its slot interval. The
owner edits durations in the dashboard.
"""
from __future__ import annotations

import re

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app_platform.config import load_json_config
from app_platform.errors import bad_request, conflict
from app_platform.timeutil import WEEKDAYS, is_valid_hhmm, to_minutes
from dbhandlers.models import Business, Service, Staff

SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
RESERVED_SLUGS = {
    "api", "static", "uploads", "signup", "signin", "login", "logout", "dashboard", "manage", "terms",
    "privacy", "forgot-password", "admin", "platform-admin", "health", "ready", "booking", "b", "www",
    "centra", "help", "support", "pricing",
}


def catalog() -> dict:
    return load_json_config("catalog")


def check_slug(db: Session, slug: str, own_business_id: int | None = None) -> str:
    slug = (slug or "").strip().lower()
    if not (3 <= len(slug) <= 60) or not SLUG_RE.match(slug):
        raise bad_request("Use 3–60 lowercase letters, numbers and hyphens for your booking link.", code="bad_slug")
    if slug in RESERVED_SLUGS:
        raise conflict("That booking link is reserved. Please choose another.", code="slug_taken")
    other = db.scalars(select(Business).where(Business.slug == slug)).first()
    if other and other.id != own_business_id:
        raise conflict("That booking link is already taken. Please choose another.", code="slug_taken")
    return slug


def _days(value, label: str) -> list[str]:
    days = [d for d in (value or []) if d in WEEKDAYS]
    if not days:
        raise bad_request(f"Choose at least one working day for {label}.")
    return sorted(set(days), key=WEEKDAYS.index)


def _hours(open_: str, close: str, label: str) -> tuple[str, str]:
    if not (is_valid_hhmm(open_) and is_valid_hhmm(close)):
        raise bad_request(f"Choose valid times for {label}.")
    if to_minutes(open_) >= to_minutes(close):
        raise bad_request(f"Closing time must be later than opening time for {label}.")
    return open_, close


def _interval(value, custom) -> int:
    if str(value) == "custom":
        try:
            n = int(custom)
        except (TypeError, ValueError):
            raise bad_request("Enter a custom slot interval of at least 5 minutes.")
        if not 5 <= n <= 480:
            raise bad_request("Enter a custom slot interval between 5 and 480 minutes.")
        return n
    if str(value) not in ("15", "30", "60"):
        raise bad_request("Choose a slot interval.")
    return int(value)


def _price_paise(raw) -> int:
    if raw in (None, ""):
        return 0
    try:
        rupees = float(raw)
    except (TypeError, ValueError):
        raise bad_request("Prices must be numbers.")
    if rupees < 0 or rupees > 10_000_000:
        raise bad_request("Enter a valid price.")
    return int(round(rupees * 100))


def _break(value) -> tuple[str, str]:
    if not value or value in ("none", "custom"):
        return "", ""
    try:
        a, b = str(value).split("-")
    except ValueError:
        return "", ""
    if is_valid_hhmm(a) and is_valid_hhmm(b) and to_minutes(a) < to_minutes(b):
        return a, b
    return "", ""


def validate_wizard(db: Session, biz: Business, state: dict) -> dict:
    """Checks everything the wizard collected. Returns a normalised dict."""
    b = state.get("business") or {}
    cat = catalog()
    types = {t: c for c in cat["categories"] for t in c["types"]}
    btype = b.get("type")
    if btype not in types:
        raise bad_request("Choose your business type.")
    name = (b.get("name") or "").strip()
    if not name:
        raise bad_request("Enter your business name.")
    city = (b.get("city") or "").strip()
    if not city:
        raise bad_request("Enter your city.")
    phone = (b.get("phone") or "").strip()
    if not phone:
        raise bad_request("Enter the business phone number.")
    slug = check_slug(db, b.get("slug", ""), biz.id)
    open_, close = _hours(b.get("open", ""), b.get("close", ""), "your business")
    days = _days(b.get("days"), "your business")

    s = state.get("services") or {}
    # Every service listed for the business's category is offered (grouped, with checkboxes).
    preset = [n for g in types[btype]["groups"] for n in g["services"]]
    custom = [c.strip()[:120] for c in (s.get("custom") or []) if str(c).strip()]
    all_names = list(dict.fromkeys(preset + custom))
    chosen = all_names if s.get("showAll") else [n for n in (s.get("selected") or []) if n in all_names]
    if not chosen:
        raise bad_request("Select at least one service.")
    interval = _interval(s.get("interval"), s.get("customInterval"))
    prices = s.get("prices") or {}
    show_pricing = bool(s.get("showPricing", True))
    # Prices are stored even when hidden from customers (deposits are still worked out from them).
    services = [{"name": n, "price_paise": _price_paise(prices.get(n)), "interval": interval} for n in chosen]

    staff = []
    for i, st in enumerate(state.get("staff") or []):
        sname = (st.get("name") or "").strip()
        if not sname:
            raise bad_request(f"Enter a name for staff member {i + 1}, or remove them.")
        s_open, s_close = _hours(st.get("start", ""), st.get("end", ""), sname)
        bs, be = _break(st.get("break"))
        iv = st.get("interval")
        staff.append({
            "name": sname[:120], "role": (st.get("role") or "").strip()[:80],
            "days": _days(st.get("days"), sname), "start": s_open, "end": s_close,
            "break_start": bs, "break_end": be,
            "interval": int(iv) if str(iv) in ("15", "30", "60") else None,
        })

    plan = state.get("plan", "Base")
    billing = state.get("billing", "monthly")
    if plan not in ("Base", "Top") or billing not in ("monthly", "pack"):
        raise bad_request("Choose a plan and billing option.")
    meta = state.get("metaVerified", "")
    if plan == "Top" and meta != "yes":
        # Decided: "No" means no Top billing; the page switches them back to Base.
        raise bad_request("Top is only available for Meta-verified businesses. Your plan has been switched to Base.",
                          code="top_requires_meta", plan="Base")
    return {
        "business": {
            "name": name[:120], "slug": slug, "category": types[btype]["name"], "business_type": btype,
            "address": (b.get("address") or "").strip()[:255], "city": city[:80], "phone": phone[:32],
            "whatsapp": (b.get("whatsapp") or "").strip()[:32], "working_days": days,
            "open_time": open_, "close_time": close, "show_pricing": show_pricing, "default_interval": interval,
        },
        "services": services, "staff": staff, "plan": plan, "billing": billing, "meta": meta == "yes",
    }


def apply_wizard(db: Session, biz: Business, clean: dict) -> None:
    """Writes the validated wizard into the draft business. Caller commits (one unit)."""
    if biz.status != "draft":
        raise conflict("This business is already set up. Use the dashboard to make changes.")
    for k, v in clean["business"].items():
        setattr(biz, k, v)
    biz.meta_verified = clean["meta"]
    db.execute(delete(Service).where(Service.business_id == biz.id))
    db.execute(delete(Staff).where(Staff.business_id == biz.id))
    for i, s in enumerate(clean["services"]):
        db.add(Service(business_id=biz.id, name=s["name"], price_paise=s["price_paise"],
                       duration_min=s["interval"], interval_min=s["interval"], sort=i))
    for st in clean["staff"]:
        db.add(Staff(business_id=biz.id, name=st["name"], role=st["role"], working_days=st["days"],
                     start_time=st["start"], end_time=st["end"], break_start=st["break_start"],
                     break_end=st["break_end"], interval_min=st["interval"]))
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise conflict("That booking link is already taken. Please choose another.", code="slug_taken")
