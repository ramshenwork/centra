"""Owner customer lookup: grouping by phone, status breakdown, date filter, history, tenant isolation."""
from tests.conftest import future_date, signup_business


def _book(c, slug, sid, d, t, name, email, phone):
    r = c.post(f"/api/public/businesses/{slug}/bookings", json={
        "services": [sid], "staff": "any", "date": d, "time": t, "name": name, "email": email, "phone": phone})
    assert r.status_code == 200, r.text
    return r.json()["booking"]["code"]


def _setup(c, email, slug):
    signup_business(c, email, slug)
    sid = c.get(f"/api/public/businesses/{slug}").json()["services"][0]["id"]
    d = future_date(4).isoformat()
    times = [s["time"] for s in c.get(f"/api/public/businesses/{slug}/availability?date={d}&services={sid}").json()["slots"]]
    return sid, d, times


def _appt_id(c, code):
    return next(a["id"] for a in c.get("/api/dashboard/state").json()["appointments"] if a["code"] == code)


def test_same_phone_is_one_customer_with_status_counts(app_client):
    c = app_client
    sid, d, times = _setup(c, "lk1@x.com", "lk-shop")
    a = _book(c, "lk-shop", sid, d, times[0], "Ram", "ram@y.com", "+91 98765 43210")
    b = _book(c, "lk-shop", sid, d, times[1], "Ram K", "ram2@y.com", "9876543210")       # other email, other phone format
    _book(c, "lk-shop", sid, d, times[2], "Sita", "sita@y.com", "9123456789")
    assert c.post(f"/api/dashboard/appointments/{_appt_id(c, b)}/status", json={"status": "cancelled"}).status_code == 200
    r = c.get("/api/dashboard/customers/lookup")
    assert r.status_code == 200
    people = {p["key"]: p for p in r.json()["customers"]}
    assert set(people) == {"9876543210", "9123456789"}
    ram = people["9876543210"]
    assert ram["total"] == 2 and ram["statuses"] == {"Confirmed": 1, "Cancelled": 1}
    # phone search ignores spaces, dashes and +91
    r = c.get("/api/dashboard/customers/lookup", params={"q": "98765 43210"})
    assert [p["key"] for p in r.json()["customers"]] == ["9876543210"]
    assert c.get("/api/dashboard/customers/lookup", params={"q": "nobody-here"}).json()["matched"] == 0


def test_date_filter_lists_customers_with_an_appointment_in_range(app_client):
    c = app_client
    sid, d, times = _setup(c, "lk2@x.com", "lk-date")
    _book(c, "lk-date", sid, d, times[0], "Ram", "ram@y.com", "9876543210")
    hit = c.get("/api/dashboard/customers/lookup", params={"date_from": d, "date_to": d}).json()
    assert hit["matched"] == 1
    miss = c.get("/api/dashboard/customers/lookup", params={"date_from": "2020-01-01", "date_to": "2020-01-31"}).json()
    assert miss["matched"] == 0
    assert c.get("/api/dashboard/customers/lookup", params={"date_from": "nonsense"}).status_code == 400
    assert c.get("/api/dashboard/customers/lookup", params={"date_from": d, "date_to": "2020-01-01"}).status_code == 400


def test_history_lists_every_booking_for_one_customer(app_client):
    c = app_client
    sid, d, times = _setup(c, "lk3@x.com", "lk-hist")
    a = _book(c, "lk-hist", sid, d, times[0], "Ram", "ram@y.com", "9876543210")
    b = _book(c, "lk-hist", sid, d, times[1], "Ram", "ram2@y.com", "98765 43210")
    r = c.get("/api/dashboard/customers/history", params={"key": "9876543210"})
    assert r.status_code == 200
    rows = r.json()["bookings"]
    assert {x["code"] for x in rows} == {a, b} and all(x["status"] == "Confirmed" and x["services"] for x in rows)
    assert c.get("/api/dashboard/customers/history", params={"key": "'; drop table"}).status_code == 400
    assert c.get("/api/dashboard/customers/history").status_code == 400


def test_lookup_never_shows_another_business(app_client):
    c = app_client
    sid, d, times = _setup(c, "lk4@x.com", "lk-mine")
    _book(c, "lk-mine", sid, d, times[0], "Ram", "ram@y.com", "9876543210")
    c.post("/api/auth/signout")
    signup_business(c, "lk5@x.com", "lk-other")
    assert c.get("/api/dashboard/customers/lookup").json()["matched"] == 0
    assert c.get("/api/dashboard/customers/history", params={"key": "9876543210"}).json()["bookings"] == []
    c.post("/api/auth/signout")
    assert c.get("/api/dashboard/customers/lookup").status_code in (401, 403)
