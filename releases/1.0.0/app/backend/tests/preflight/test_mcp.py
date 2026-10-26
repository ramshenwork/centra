"""MCP (AI assistant booking) over real HTTP JSON-RPC, against a throwaway database."""
import json
from datetime import timedelta

from fastapi.testclient import TestClient

from tests.conftest import future_date, signup_business

H = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def rpc(c, method, params=None, headers=None):
    r = c.post("/mcp", headers=headers or H, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})
    return r


def call(c, tool, **args):
    r = rpc(c, "tools/call", {"name": tool, "arguments": args})
    assert r.status_code == 200, r.text
    res = r.json()["result"]
    text = res["content"][0]["text"]
    return res.get("isError", False), (text if res.get("isError") else json.loads(text))


def test_mcp_search_book_and_dashboard(app_client):
    signup_business(app_client, "ai@x.com", "ai-salon")
    with TestClient(app_client.app) as c:
        names = [t["name"] for t in rpc(c, "tools/list").json()["result"]["tools"]]
        assert names == ["search_businesses", "find_business", "get_business", "check_availability", "create_booking"]
        err, found = call(c, "search_businesses", query="manicure", area="Madhapur, Hyderabad")
        assert not err and found["businesses"][0]["slug"] == "ai-salon" and found["area_matched"]
        err, biz = call(c, "get_business", slug="ai-salon")
        sid = biz["services"][0]["id"]
        d = future_date(3).isoformat()
        err, av = call(c, "check_availability", slug="ai-salon", service_ids=[sid], date=d)
        t = av["slots"][0]["time"]
        err, booked = call(c, "create_booking", slug="ai-salon", service_ids=[sid], date=d, time=t,
                           customer_name="Asha", customer_email="asha@y.com", customer_phone="9876500000")
        assert not err and booked["status"] == "Confirmed" and "/manage?token=" in booked["manage_link"]
        err, again = call(c, "create_booking", slug="ai-salon", service_ids=[sid], date=d, time=t,
                          customer_name="Bob", customer_email="bob@y.com", customer_phone="9876500001")
        assert err and "taken" in again  # double booking blocked
    state = app_client.get("/api/dashboard/state").json()
    assert any(a["code"] == booked["booking_code"] and a["source"] == "ai" for a in state["appointments"])
    assert any(n["title"] == "New booking (AI assistant)" for n in state["notifications"])


def test_mcp_hides_draft_and_expired(app_client):
    signup_business(app_client, "exp@x.com", "old-salon")
    from dbhandlers.database import session_factory, utcnow
    from dbhandlers.models import Business
    with session_factory()() as db:
        b = db.query(Business).filter_by(slug="old-salon").one()
        b.plan_active_until = utcnow() - timedelta(days=1)
        db.commit()
    with TestClient(app_client.app) as c:
        err, found = call(c, "search_businesses", query="salon", area="Hyderabad")
        assert found["count"] == 0
        err, msg = call(c, "get_business", slug="old-salon")
        assert err and "No bookable" in msg


def test_mcp_deposit_and_errors(app_client):
    staff = [{"name": "Ravi", "role": "", "days": ["Mon", "Tue", "Thu", "Fri", "Sat", "Sun"],
              "start": "09:00", "end": "19:00", "break": "none", "interval": "30"}]
    signup_business(app_client, "dep2@x.com", "dep-salon", staff=staff)
    app_client.put("/api/dashboard/settings", json={"deposits": True, "depositPercent": 10})
    with TestClient(app_client.app) as c:
        err, biz = call(c, "get_business", slug="dep-salon")
        sid = biz["services"][0]["id"]
        ravi = [s["id"] for s in biz["staff"] if s["name"] == "Ravi"][0]
        wed = future_date(1)
        while wed.weekday() != 2:
            wed += timedelta(days=1)
        err, av = call(c, "check_availability", slug="dep-salon", service_ids=[sid], date=wed.isoformat(), staff=ravi)
        assert av["slots"] == [] and "unavailable" in av["note"]
        err, msg = call(c, "check_availability", slug="dep-salon", service_ids=[sid], date="28-10-2026")
        assert err and "YYYY-MM-DD" in msg
        d = (wed + timedelta(days=1)).isoformat()
        t = call(c, "check_availability", slug="dep-salon", service_ids=[sid], date=d)[1]["slots"][0]["time"]
        err, booked = call(c, "create_booking", slug="dep-salon", service_ids=[sid], date=d, time=t,
                           customer_name="Asha", customer_email="a2@y.com", customer_phone="9876500002")
        assert booked["status"] == "Awaiting payment" and booked["deposit_due_inr"] == 50.0


def test_mcp_rejects_foreign_host(app_client):
    with TestClient(app_client.app) as c:
        r = rpc(c, "tools/list", headers={**H, "Host": "evil.example.com"})
        assert r.status_code == 421


def test_mcp_asks_for_location_first(app_client):
    signup_business(app_client, "loc@x.com", "loc-salon")
    with TestClient(app_client.app) as c:
        err, res = call(c, "search_businesses", query="haircut")
        assert res["needs_location"] is True and res["count"] == 0
        err, res = call(c, "search_businesses", query="salon", area="Kukatpally, Hyderabad")
        assert res["count"] == 1


def test_find_business_by_name_typos_and_clashes(app_client):
    signup_business(app_client, "g1@x.com", "glow-madhapur", name="Glow Salon", areas=["Madhapur", "Hitech City"])
    app_client.post("/api/auth/signout")
    signup_business(app_client, "g2@x.com", "glow-kukatpally", name="Glow Salon & Spa", areas=["Kukatpally", "KPHB"])
    app_client.post("/api/auth/signout")
    signup_business(app_client, "o1@x.com", "olive-cuts", name="Olive Cuts", areas=["Gachibowli"])
    with TestClient(app_client.app) as c:
        err, r = call(c, "find_business", name="glow saloon")
        assert r["status"] == "ambiguous" and {m["area"] for m in r["matches"]} == {"Madhapur", "Kukatpally"}
        err, r = call(c, "find_business", name="glow salon", area="KPHB, Hyderabad")
        assert r["status"] == "found" and r["business"]["slug"] == "glow-kukatpally"
        err, r = call(c, "find_business", name="olive")
        assert r["status"] == "found" and r["business"]["area"] == "Gachibowli"
        err, r = call(c, "find_business", name="Blue Moon Barbers")
        assert r["status"] == "not_found"
        err, r = call(c, "search_businesses", area="Hitech City, Hyderabad", query="salon")
        assert r["businesses"][0]["slug"] == "glow-madhapur" and r["businesses"][0]["match"] == "listed area"


def test_areas_required_and_limited(app_client):
    from tests.conftest import last_code
    c = app_client
    c.post("/api/signup/start", json={"name": "O", "email": "ar@x.com", "password": "Passw0rd!", "confirm": "Passw0rd!", "terms": True})
    c.post("/api/signup/verify", json={"email": "ar@x.com", "code": last_code("ar@x.com")})
    from tests.conftest import wizard_state
    st = wizard_state("area-shop")
    st["business"]["areas"] = ["", "", ""]
    assert c.post("/api/signup/checkout", json={"state": st}).json()["error"]["code"] == "area_required"
    st["business"]["areas"] = ["A1", "A2", "A3", "A4"]
    assert c.post("/api/signup/checkout", json={"state": st}).json()["error"]["code"] == "too_many_areas"
