"""
services/mcp/server.py
----------------------
CENTRA's MCP server: lets AI assistants (Claude, ChatGPT) find CENTRA
businesses and book appointments for their users.

Served by the main app at  POST /mcp  (MCP "streamable HTTP", stateless,
JSON responses). No login: like the public booking page, it only exposes
public information and can only create bookings.

Who is visible: businesses with a PAID plan (status active and plan not
expired), Base or Top. Drafts and expired plans are invisible.

The 4 tools all go through the same booking engine as the booking page
(services/booking/engine.py), so every rule applies: service interval
wins, staff hours / breaks / leave / suspensions, closures, the rolling
2-month window, deposit holds and double-booking protection.

Location: the assistant passes the customer's city/area as text; this
server matches it against business name, city, address and type and
returns full addresses. The assistant itself judges which is nearest
(RR's decision: no maps API for now).
"""
from __future__ import annotations

import re
from datetime import date as date_cls
from difflib import SequenceMatcher

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from sqlalchemy import select

from app_platform.config import get_settings
from app_platform.errors import AppError
from app_platform.logger import get_logger
from app_platform.timeutil import label_12h
from dbhandlers.database import session_factory, utcnow
from dbhandlers.models import Business
from middlewares.rate_limit import check as rate_check
from services.booking import engine

log = get_logger("mcp")

INSTRUCTIONS = """\
CENTRA lets you book appointments at independent service businesses in India
(salons, spas, dental clinics, car detailing and car service, photography studios).

A. If the user NAMES a business ("book at Glow Salon"):
   call find_business with that name (and their area if they gave one).
   - status "found": continue with that business.
   - status "ambiguous": several businesses match; ask the user which one by reading out the areas listed
     (e.g. "Glow Salon in Madhapur or in Kukatpally?"), then use the slug of the one they pick.
   - status "not_found": say so, and offer to search near them instead (B).
B. If the user wants something NEARBY ("a haircut near me"):
   FIRST get their current location: area/locality and city (e.g. "Kukatpally, Hyderabad"). If your app gives
   you an approximate location, confirm it with the user. Then call search_businesses with it as `area`.
   Businesses that list that area come first ("match": "listed area"); suggest those first and say distances
   are estimated from addresses.

Then:
1. get_business with the chosen slug to see services (ids, durations, prices if shown), staff and closed dates.
2. check_availability with the service ids and a date (YYYY-MM-DD, India time) to get free start times.
3. BEFORE create_booking, read back the business, area, services, date, time and staff and get the user's explicit
   confirmation, plus their name, email and phone number. Never invent these details.
4. create_booking. Tell the user the booking code and give them the manage link (view, reschedule or cancel).
   If a deposit is required, tell them the slot is held for a few minutes and they must open the link to pay.

All times are 24-hour HH:MM in India time (Asia/Kolkata). Bookings can be made up to 2 months ahead.
Only use information returned by these tools; if something is not listed, say you don't know.
"""


def _visible_query(now):
    return select(Business).where(
        Business.status == "active", Business.slug.is_not(None),
        Business.plan_active_until.is_not(None), Business.plan_active_until > now,
    )


def _visible_business(db, slug: str, now) -> Business:
    biz = db.scalars(_visible_query(now).where(Business.slug == (slug or "").strip().lower())).first()
    if biz is None:
        raise ToolError(f"No bookable CENTRA business found with slug '{slug}'. Use search_businesses first.")
    return biz


def _parse_date(value: str) -> date_cls:
    try:
        return date_cls.fromisoformat((value or "").strip())
    except ValueError:
        raise ToolError("Dates must be in YYYY-MM-DD format, e.g. 2026-10-28.")


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower().replace("&", " and ")).split())


def name_score(query: str, name: str) -> float:
    """How well a customer's spelling of a name matches a business name (0..1). Forgives typos and partials."""
    q, n = _norm(query), _norm(name)
    if not q or not n:
        return 0.0
    if q == n:
        return 1.0
    if q in n or n in q:
        return 0.9
    ratio = SequenceMatcher(None, q, n).ratio()
    qt, nt = set(q.split()), set(n.split())
    tokens = len(qt & nt) / len(qt)
    fuzzy_tokens = sum(1 for w in qt if any(SequenceMatcher(None, w, x).ratio() >= 0.8 for x in nt)) / len(qt)
    if fuzzy_tokens == 1.0:
        return max(ratio, 0.9)  # every word the customer said is in the name (typos allowed)
    return max(ratio, 0.85 * tokens, 0.8 * fuzzy_tokens)


NAME_MATCH = 0.72


def _area_tokens(area: str) -> list[str]:
    return [t for t in _norm(area).split() if len(t) > 2]


def area_match(biz: Business, area: str) -> str:
    """'listed area' (one of the owner's chosen areas) > 'address' > 'same city' > ''."""
    tokens = _area_tokens(area)
    if not tokens:
        return ""
    city = _norm(biz.city)
    local = [t for t in tokens if t != city] or tokens
    listed = " | ".join(_norm(a) for a in (biz.areas or []))
    if listed and any(t in listed for t in local):
        return "listed area"
    if any(t in _norm(biz.address) for t in local):
        return "address"
    if city and any(t == city or t in city for t in tokens):
        return "same city"
    return ""


def _limit(bucket: str, limit: int, per: int) -> None:
    try:
        rate_check(f"mcp:{bucket}", limit, per)
    except AppError:
        raise ToolError("Too many requests right now. Please wait a minute and try again.")


def _summary(biz: Business) -> dict:
    s = get_settings()
    return {
        "slug": biz.slug, "name": biz.name, "type": biz.business_type, "category": biz.category,
        "area": (biz.areas or [biz.city])[0], "search_areas": biz.areas or [],
        "address": ", ".join(x for x in [biz.address, biz.city] if x), "city": biz.city, "phone": biz.phone,
        "hours": f"{label_12h(biz.open_time)} – {label_12h(biz.close_time)}",
        "open_days": biz.working_days or [], "booking_page": f"{s.base_url}/{biz.slug}",
    }


def build_server() -> MCPServer:
    mcp = MCPServer(name="CENTRA", title="CENTRA bookings", version="1.0.0", instructions=INSTRUCTIONS,
                    website_url=get_settings().base_url)

    @mcp.tool(description="Find bookable CENTRA businesses NEAR the customer. `area` is REQUIRED: the customer's "
                          "current area/locality and city (ask them first). `query` is optional free text: a service "
                          "or type such as 'haircut', 'salon', 'spa', 'dental', 'car detailing', 'photography'. "
                          "If the customer names a specific business, use find_business instead.")
    def search_businesses(area: str = "", query: str = "", limit: int = 10) -> dict:
        _limit("search", 120, 60)
        now = utcnow()
        limit = max(1, min(int(limit or 10), 25))
        if not _area_tokens(area):
            return {"businesses": [], "count": 0, "needs_location": True,
                    "note": "Ask the customer for their current area/locality and city, then search again with it "
                            "as `area`. If they named a specific business, use find_business."}
        words = [w for w in _norm(query).split() if len(w) > 1]
        rank = {"listed area": 0, "address": 1, "same city": 2}
        with session_factory()() as db:
            rows = db.scalars(_visible_query(now)).all()
            svc_names: dict[int, list[str]] = {}
            if words and rows:
                from dbhandlers.models import Service

                for bid, name in db.execute(select(Service.business_id, Service.name).where(
                        Service.business_id.in_([b.id for b in rows]), Service.shown.is_(True))).all():
                    svc_names.setdefault(bid, []).append(_norm(name))
            found = []
            for b in rows:
                where = area_match(b, area)
                if not where:
                    continue
                if words:
                    hay = " ".join([_norm(b.name), _norm(b.business_type), _norm(b.category)] + svc_names.get(b.id, []))
                    if not any(w in hay for w in words):
                        continue
                found.append((rank[where], where, b))
            found.sort(key=lambda x: x[0])
            out = [{**_summary(b), "match": where} for _, where, b in found[:limit]]
        log.info("mcp search results=%s", len(out))
        return {"businesses": out, "count": len(out),
                "area_matched": any(x["match"] != "same city" for x in out),
                "note": "Suggest 'listed area' matches first; use the addresses to judge which is nearest." if out else
                        "No CENTRA businesses found for that area. Try a nearby area, the city name, or another service."}

    @mcp.tool(description="Find a business the customer NAMED (spelling mistakes and partial names are fine). "
                          "Optionally pass the customer's `area` to narrow it down. Returns status 'found', "
                          "'ambiguous' (ask the customer which area) or 'not_found'.")
    def find_business(name: str, area: str = "") -> dict:
        _limit("find", 120, 60)
        now = utcnow()
        with session_factory()() as db:
            rows = db.scalars(_visible_query(now)).all()
            scored = sorted(((name_score(name, b.name), b) for b in rows), key=lambda x: -x[0])
            scored = [(sc, b) for sc, b in scored if sc >= NAME_MATCH]
            if scored:
                best = scored[0][0]
                scored = [(sc, b) for sc, b in scored if sc >= best - 0.1]
            cands = [b for _, b in scored]
            narrowed = False
            if area and len(cands) > 1:
                in_area = [b for b in cands if area_match(b, area) in ("listed area", "address")]
                if in_area:
                    cands, narrowed = in_area, True
            if not cands:
                return {"status": "not_found", "note": f"No CENTRA business matches '{name}'. Offer to search near the "
                                                       "customer's location with search_businesses instead."}
            if len(cands) == 1:
                return {"status": "found", "business": _summary(cands[0])}
            matches = [_summary(b) for b in cands[:10]]
            areas = ", ".join(f"{m['name']} in {m['area']}" for m in matches)
            return {"status": "ambiguous", "matches": matches,
                    "note": f"Several businesses match{' in that area' if narrowed else ''}: {areas}. "
                            "Ask the customer which one (by area) and use its slug."}

    @mcp.tool(description="Get one business's services (use the ids when checking availability or booking), "
                          "durations, prices (only if the business shows them), staff, opening hours, closed "
                          "dates in the booking window and deposit/cancellation rules.")
    def get_business(slug: str) -> dict:
        _limit("business", 120, 60)
        now = utcnow()
        from services.booking.router import public_business

        with session_factory()() as db:
            biz = _visible_business(db, slug, now)
            data = public_business(db, biz, now)
            db.commit()
        return {
            **_summary(biz),
            "services": [{"id": s["id"], "name": s["name"], "minutes": s["minutes"], "price_inr": s["price"]}
                         for s in data["services"]],
            "prices_shown": data["prices"], "multiple_services_per_booking": data["allowMultiple"],
            "staff": [{"id": str(s["id"]), "name": s["name"], "role": s["role"]} for s in data["staff"]],
            "booking_window": data["window"], "closed_dates": data["closedDates"],
            "deposit": {"required": data["depositEnabled"], "percent": data["depositPercent"],
                        "full_refund_if_cancelled_hours_before": data["refundWindowHours"]},
            "policies": data["terms"],
        }

    @mcp.tool(description="Free start times for the chosen services on a date (YYYY-MM-DD, India time). "
                          "staff is a staff id from get_business, or 'any'. Times are 24-hour HH:MM.")
    def check_availability(slug: str, service_ids: list[int], date: str, staff: str = "any") -> dict:
        _limit("availability", 120, 60)
        now = utcnow()
        d = _parse_date(date)
        with session_factory()() as db:
            biz = _visible_business(db, slug, now)
            try:
                data = engine.availability(db, biz, [int(x) for x in service_ids], str(staff or "any"), d, now)
            except AppError as e:
                raise ToolError(e.message)
            db.commit()
        if data.get("outside_window"):
            return {"date": d.isoformat(), "slots": [], "note": "Outside the 2-month booking window."}
        if data.get("closed"):
            return {"date": d.isoformat(), "slots": [], "note": "The business is closed on this date."}
        slots = data["slots"]
        note = "" if slots else ("This staff member is unavailable on this date; try 'any' or another date."
                                 if str(staff) != "any" else "No free times on this date; try another day.")
        return {"date": d.isoformat(), "slots": [{"time": s["time"], "label": s["label"], "period": s["period"]}
                                                 for s in slots], "note": note}

    @mcp.tool(description="Book an appointment. Only call after the user has confirmed the business, services, "
                          "date, time and staff, and given their name, email and phone. date is YYYY-MM-DD and "
                          "time is HH:MM (24-hour, India time) and must be one of the times from "
                          "check_availability. Returns the booking code and a manage link for the customer.")
    def create_booking(slug: str, service_ids: list[int], date: str, time: str, customer_name: str,
                       customer_email: str, customer_phone: str, staff: str = "any", note: str = "") -> dict:
        _limit("book", 30, 600)
        now = utcnow()
        d = _parse_date(date)
        s = get_settings()
        with session_factory()() as db:
            biz = _visible_business(db, slug, now)
            nb = engine.NewBooking(service_ids=[int(x) for x in service_ids], staff=str(staff or "any"), date=d,
                                   time=(time or "").strip(), name=customer_name, email=customer_email,
                                   phone=customer_phone, note=(note or "")[:500], source="ai")
            try:
                b, token = engine.create_booking(db, biz, nb, now)
                payment = engine.after_create(db, biz, b, token, now)
            except AppError as e:
                db.rollback()
                raise ToolError(e.message)
            view = engine.booking_public_view(db, biz, b)
            db.commit()
        log.info("mcp booking created business=%s booking=%s status=%s", biz.id, view["code"], view["status"])
        result = {
            "booking_code": view["code"], "status": view["status_label"], "business": biz.name,
            "address": ", ".join(x for x in [biz.address, biz.city] if x), "business_phone": biz.phone,
            "date": view["date"], "time": view["time"], "time_label": label_12h(view["time"]),
            "staff": view["staff"], "services": [x["name"] for x in view["services"]],
            "manage_link": f"{s.base_url}/manage?token={token}",
        }
        if payment:
            result["deposit_due_inr"] = payment["amount"] / 100
            result["next_step"] = ("A deposit is required. The slot is held for a few minutes: the customer must "
                                   "open the manage link now and pay the deposit to confirm.")
        else:
            result["next_step"] = "Confirmed. The customer can view, reschedule or cancel with the manage link."
        return result

    return mcp


def transport_security() -> TransportSecuritySettings:
    """Only accept requests addressed to our own host (protects against DNS rebinding)."""
    from urllib.parse import urlparse

    host = urlparse(get_settings().base_url).netloc
    hosts = {host, "localhost:*", "127.0.0.1:*", "testserver"}
    origins = {get_settings().base_url, "https://claude.ai", "https://chatgpt.com", "https://chat.openai.com",
               "http://localhost:*", "http://127.0.0.1:*"}
    return TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=sorted(hosts),
                                     allowed_origins=sorted(origins))
