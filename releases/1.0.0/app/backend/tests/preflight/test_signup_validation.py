"""Mandatory street address: wizard checkout and dashboard profile."""
from tests.conftest import last_code, signup_business, wizard_state


def _verified_owner(c, email):
    r = c.post("/api/signup/start", json={"name": "Owner", "email": email, "phone": "", "password": "Passw0rd!",
                                          "confirm": "Passw0rd!", "terms": True})
    assert r.status_code == 200, r.text
    r = c.post("/api/signup/verify", json={"email": email, "code": last_code(email)})
    assert r.status_code == 200, r.text


def test_checkout_requires_street_address(app_client):
    c = app_client
    _verified_owner(c, "addr@x.com")
    st = wizard_state("addr-shop")
    for blank in ("", "   "):
        st["business"]["address"] = blank
        r = c.post("/api/signup/checkout", json={"state": st})
        assert r.status_code == 400 and "street address" in r.json()["error"]["message"].lower()
    st["business"]["address"] = "12 MG Road"
    assert c.post("/api/signup/checkout", json={"state": st}).status_code == 200


def test_profile_requires_street_address(app_client):
    c = app_client
    signup_business(c, "prof@x.com", "prof-shop")
    body = {"name": "Test Salon", "city": "Hyderabad", "address": "1 Road", "phone": "+91 9876543210", "slug": "prof-shop",
            "open": "09:00", "close": "19:00", "days": ["Mon", "Tue"]}
    assert c.put("/api/dashboard/profile", json=body).status_code == 200
    r = c.put("/api/dashboard/profile", json={**body, "address": "  "})
    assert r.status_code == 400 and "street address" in r.json()["error"]["message"].lower()
