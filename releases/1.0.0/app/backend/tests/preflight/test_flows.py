"""End-to-end API flows against a throwaway database with mock payments."""
from tests.conftest import future_date, last_code, signup_business, wizard_state


def _first_service(client, slug):
    biz = client.get(f"/api/public/businesses/{slug}").json()
    return biz, biz["services"][0]["id"]


def test_signup_to_live_booking_page(app_client):
    c = app_client
    signup_business(c, "a@x.com", "salon-a")
    assert c.get("/api/dashboard/state").status_code == 200
    biz, sid = _first_service(c, "salon-a")
    assert biz["name"] == "Test Salon" and len(biz["services"]) == 2
    assert c.get("/salon-a").status_code == 200
    assert c.get("/no-such-business").status_code == 404


def test_draft_business_page_not_live_until_paid(app_client):
    c = app_client
    c.post("/api/signup/start", json={"name": "O", "email": "d@x.com", "password": "Passw0rd!", "confirm": "Passw0rd!", "terms": True})
    c.post("/api/signup/verify", json={"email": "d@x.com", "code": last_code("d@x.com")})
    r = c.post("/api/signup/checkout", json={"state": wizard_state("draft-shop")})
    assert r.status_code == 200
    assert c.get("/api/public/businesses/draft-shop").status_code == 404
    assert c.get("/api/dashboard/state").status_code == 409  # sign-up not finished


def test_top_requires_meta_verified(app_client):
    c = app_client
    c.post("/api/signup/start", json={"name": "O", "email": "t@x.com", "password": "Passw0rd!", "confirm": "Passw0rd!", "terms": True})
    c.post("/api/signup/verify", json={"email": "t@x.com", "code": last_code("t@x.com")})
    r = c.post("/api/signup/checkout", json={"state": wizard_state("top-shop", plan="Top", meta="no")})
    assert r.status_code == 400 and r.json()["error"]["code"] == "top_requires_meta"


def test_booking_and_double_booking_blocked(app_client):
    c = app_client
    signup_business(c, "b@x.com", "salon-b")
    _, sid = _first_service(c, "salon-b")
    d = future_date().isoformat()
    slots = c.get(f"/api/public/businesses/salon-b/availability?date={d}&services={sid}").json()["slots"]
    t = slots[0]["time"]
    body = {"services": [sid], "staff": "any", "date": d, "time": t, "name": "Cust", "email": "c@y.com", "phone": "9876543210"}
    r1 = c.post("/api/public/businesses/salon-b/bookings", json=body)
    assert r1.status_code == 200 and r1.json()["booking"]["status"] == "confirmed"
    r2 = c.post("/api/public/businesses/salon-b/bookings", json={**body, "email": "other@y.com", "phone": "9999999999"})
    assert r2.status_code == 409
    slots2 = c.get(f"/api/public/businesses/salon-b/availability?date={d}&services={sid}").json()["slots"]
    assert t not in [s["time"] for s in slots2]
    tok = r1.json()["token"]
    assert c.get(f"/api/public/manage?token={tok}").status_code == 200


def test_tenant_isolation(app_client):
    c = app_client
    signup_business(c, "one@x.com", "shop-one")
    _, sid_one = _first_service(c, "shop-one")
    c.post("/api/auth/signout")
    signup_business(c, "two@x.com", "shop-two")
    r = c.put(f"/api/dashboard/services/{sid_one}", json={"name": "hacked", "price": 1, "duration": 30, "interval": 30})
    assert r.status_code == 404
    assert all(s["name"] != "hacked" for s in c.get("/api/dashboard/state").json()["services"])


def test_deposit_flow_and_customer_refund(app_client):
    c = app_client
    signup_business(c, "dep@x.com", "dep-shop")
    assert c.put("/api/dashboard/settings", json={"deposits": True, "depositPercent": 10, "refundWindow": 24}).status_code == 200
    _, sid = _first_service(c, "dep-shop")
    d = future_date(5).isoformat()
    t = c.get(f"/api/public/businesses/dep-shop/availability?date={d}&services={sid}").json()["slots"][0]["time"]
    r = c.post("/api/public/businesses/dep-shop/bookings", json={"services": [sid], "staff": "any", "date": d, "time": t,
                                                                  "name": "Cust", "email": "p@y.com", "phone": "9876543210"})
    data = r.json()
    assert data["booking"]["status"] == "awaiting_payment" and data["payment"]["amount"] == 5000  # 10% of ₹500
    assert c.post("/api/payments/mock-pay", json={"order_id": data["payment"]["order_id"]}).json()["status"] == "confirmed"
    r = c.post("/api/public/manage/cancel", json={"token": data["token"]})
    assert r.json()["refunded"] is True and r.json()["status"] == "refunded"


def test_signin_lockout_and_reset_keeps_lock(app_client):
    c = app_client
    signup_business(c, "lock@x.com", "lock-shop")
    c.post("/api/auth/signout")
    codes = [c.post("/api/auth/signin", json={"email": "lock@x.com", "password": "wrong"}) for _ in range(5)]
    assert [r.status_code for r in codes] == [401, 401, 401, 401, 423]
    assert "attempts_left" not in codes[1].json()["error"] and codes[3].json()["error"]["attempts_left"] == 1
    assert c.post("/api/auth/signin", json={"email": "lock@x.com", "password": "Passw0rd!"}).status_code == 423
    assert c.post("/api/auth/forgot/request", json={"email": "nobody@x.com"}).status_code == 404
    assert c.post("/api/auth/forgot/request", json={"email": "lock@x.com"}).status_code == 200
    r = c.post("/api/auth/forgot/reset", json={"email": "lock@x.com", "code": last_code("lock@x.com"),
                                               "password": "N3wPass!x", "confirm": "N3wPass!x"})
    assert r.status_code == 200 and r.json()["still_locked"] is True
    assert c.post("/api/auth/signin", json={"email": "lock@x.com", "password": "N3wPass!x"}).status_code == 423


def test_csrf_header_required(app_client):
    from fastapi.testclient import TestClient
    bare = TestClient(app_client.app)
    assert bare.post("/api/auth/signin", json={"email": "a@b.co", "password": "x"}).status_code == 403


def test_staff_break_respected(app_client):
    c = app_client
    staff = [{"name": "Ravi", "role": "Stylist", "days": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
              "start": "10:00", "end": "14:00", "break": "12:00-13:00", "interval": "60"}]
    signup_business(c, "st@x.com", "staff-shop", staff=staff)
    biz, sid = _first_service(c, "staff-shop")
    ravi = [s for s in biz["staff"] if s["name"] == "Ravi"][0]["id"]
    d = future_date().isoformat()
    times = [s["time"] for s in c.get(f"/api/public/businesses/staff-shop/availability?date={d}&services={sid}&staff={ravi}").json()["slots"]]
    assert times == ["10:00", "10:30", "11:00", "11:30", "13:00", "13:30"]


def test_fixed_test_otp_code(app_client, monkeypatch):
    from app_platform import config
    monkeypatch.setenv("TEST_OTP_CODE", "454545")
    config.reset_settings_cache()
    c = app_client
    r = c.post("/api/signup/start", json={"name": "O", "email": "fixed@x.com", "password": "Passw0rd!",
                                          "confirm": "Passw0rd!", "terms": True})
    assert r.status_code == 200
    assert c.post("/api/signup/verify", json={"email": "fixed@x.com", "code": "454545"}).status_code == 200


def test_manual_booking_warns_on_staff_day_off_or_leave(app_client):
    from datetime import timedelta
    c = app_client
    staff = [{"name": "Ravi", "role": "", "days": ["Mon", "Tue", "Thu", "Fri", "Sat", "Sun"],
              "start": "09:00", "end": "19:00", "break": "none", "interval": "30"}]
    signup_business(c, "off@x.com", "off-shop", staff=staff)
    biz = c.get("/api/public/businesses/off-shop").json()
    sid = biz["services"][0]["id"]
    ravi = [s for s in biz["staff"] if s["name"] == "Ravi"][0]["id"]
    wed = future_date(1)
    while wed.weekday() != 2:
        wed += timedelta(days=1)
    body = {"customer": "B", "phone": "9876543211", "service_ids": [sid], "staff": str(ravi), "time": "10:00"}
    r = c.post("/api/dashboard/appointments", json={**body, "date": wed.isoformat()})
    assert r.status_code == 409 and "Staff member unavailable that day" in r.json()["error"]["message"]
    r = c.post("/api/dashboard/appointments", json={**body, "date": wed.isoformat(), "override": True})
    assert r.status_code == 200  # owner confirmed the warning
    thu = (wed + timedelta(days=1)).isoformat()
    assert c.post("/api/dashboard/appointments", json={**body, "date": thu}).status_code == 200
    fri = (wed + timedelta(days=2)).isoformat()
    c.post(f"/api/dashboard/staff/{ravi}/leave", json={"date": fri, "reason": "holiday"})
    assert c.post("/api/dashboard/appointments", json={**body, "date": fri}).status_code == 409
