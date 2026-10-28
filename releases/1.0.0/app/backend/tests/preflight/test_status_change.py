"""Owner status changes: a wrong tap can be undone, money-settled bookings stay locked."""
from tests.conftest import future_date, signup_business


def _setup(c, email, slug, deposits=False):
    signup_business(c, email, slug)
    if deposits:
        assert c.put("/api/dashboard/settings", json={"deposits": True, "depositPercent": 10, "refundWindow": 24}).status_code == 200
    biz = c.get(f"/api/public/businesses/{slug}").json()
    sid = biz["services"][0]["id"]
    d = future_date(5).isoformat()
    slots = c.get(f"/api/public/businesses/{slug}/availability?date={d}&services={sid}").json()["slots"]
    return sid, d, [s["time"] for s in slots]


def _book(c, slug, sid, d, t, email="c@y.com", phone="9876543210", pay=False):
    r = c.post(f"/api/public/businesses/{slug}/bookings", json={
        "services": [sid], "staff": "any", "date": d, "time": t, "name": "Cust", "email": email, "phone": phone})
    assert r.status_code == 200, r.text
    data = r.json()
    if pay:
        assert c.post("/api/payments/mock-pay", json={"order_id": data["payment"]["order_id"]}).status_code == 200
    return data["booking"]["code"]


def _row(c, code):
    for a in c.get("/api/dashboard/state").json()["appointments"]:
        if a["code"] == code:
            return a
    raise AssertionError(f"booking {code} not in dashboard state")


def _set(c, code, status):
    return c.post(f"/api/dashboard/appointments/{_row(c, code)['id']}/status", json={"status": status})


def test_no_show_and_completed_can_be_undone(app_client):
    c = app_client
    sid, d, times = _setup(c, "u1@x.com", "undo-shop")
    code = _book(c, "undo-shop", sid, d, times[0])
    assert _set(c, code, "no_show").status_code == 200 and _row(c, code)["status"] == "No-show"
    assert _set(c, code, "confirmed").status_code == 200 and _row(c, code)["status"] == "Confirmed"
    assert _set(c, code, "completed").status_code == 200 and _row(c, code)["status"] == "Completed"
    assert _set(c, code, "no_show").status_code == 200 and _row(c, code)["status"] == "No-show"
    assert _set(c, code, "completed").status_code == 200 and _row(c, code)["status"] == "Completed"


def test_same_status_and_invalid_status_are_refused(app_client):
    c = app_client
    sid, d, times = _setup(c, "u2@x.com", "same-shop")
    code = _book(c, "same-shop", sid, d, times[0])
    assert _set(c, code, "confirmed").status_code == 400          # already confirmed
    assert _set(c, code, "refunded").status_code == 400           # not something the owner can set
    assert _set(c, code, "nonsense").status_code == 400


def test_cancelled_booking_can_be_reinstated_and_relabelled(app_client):
    c = app_client
    sid, d, times = _setup(c, "u3@x.com", "back-shop")
    code = _book(c, "back-shop", sid, d, times[0])
    assert _set(c, code, "cancelled").status_code == 200 and _row(c, code)["status"] == "Cancelled"
    assert _set(c, code, "rejected").status_code == 200 and _row(c, code)["status"] == "Rejected"
    r = _set(c, code, "confirmed")
    assert r.status_code == 200 and r.json()["status"] == "Confirmed" and "notified" in r.json()["note"]
    assert _row(c, code)["status"] == "Confirmed"


def test_reinstating_is_refused_when_the_time_was_taken(app_client):
    c = app_client
    sid, d, times = _setup(c, "u4@x.com", "clash-shop")
    first = _book(c, "clash-shop", sid, d, times[0])
    assert _set(c, first, "cancelled").status_code == 200
    _book(c, "clash-shop", sid, d, times[0], email="other@y.com", phone="9999999999")   # someone takes the freed slot
    r = _set(c, first, "confirmed")
    assert r.status_code == 409 and r.json()["error"]["code"] == "slot_taken"
    assert _row(c, first)["status"] == "Cancelled"                 # unchanged


def test_no_show_forfeits_deposit_and_undo_restores_it(app_client):
    c = app_client
    sid, d, times = _setup(c, "u5@x.com", "dep-undo", deposits=True)
    code = _book(c, "dep-undo", sid, d, times[0], pay=True)
    assert _row(c, code)["status"] == "Confirmed" and _row(c, code)["deposit"] == "Paid"
    assert _set(c, code, "no_show").status_code == 200 and _row(c, code)["deposit"] == "Forfeited"
    assert _set(c, code, "confirmed").status_code == 200
    assert _row(c, code)["status"] == "Confirmed" and _row(c, code)["deposit"] == "Paid"


def test_settled_bookings_are_locked(app_client):
    c = app_client
    sid, d, times = _setup(c, "u6@x.com", "lock-shop", deposits=True)
    code = _book(c, "lock-shop", sid, d, times[0], pay=True)
    assert _set(c, code, "cancelled").status_code == 200           # owner cancel refunds the deposit
    assert _row(c, code)["status"] == "Refunded"
    r = _set(c, code, "confirmed")
    assert r.status_code == 400 and r.json()["error"]["code"] == "status_locked"


def test_unpaid_hold_cannot_be_completed_or_marked_no_show(app_client):
    c = app_client
    sid, d, times = _setup(c, "u7@x.com", "hold-shop", deposits=True)
    code = _book(c, "hold-shop", sid, d, times[0], pay=False)      # awaiting payment
    assert _row(c, code)["status"] == "Awaiting payment"
    assert _set(c, code, "completed").status_code == 400
    assert _set(c, code, "no_show").status_code == 400
    assert _set(c, code, "confirmed").status_code == 200


def test_other_business_cannot_change_status(app_client):
    c = app_client
    sid, d, times = _setup(c, "u8@x.com", "mine-shop")
    code = _book(c, "mine-shop", sid, d, times[0])
    bid = _row(c, code)["id"]
    c.post("/api/auth/signout")
    signup_business(c, "u9@x.com", "other-shop")
    r = c.post(f"/api/dashboard/appointments/{bid}/status", json={"status": "no_show"})
    assert r.status_code == 404
