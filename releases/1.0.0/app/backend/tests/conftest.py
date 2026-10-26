"""
Shared test setup: a throwaway SQLite database, console email (codes are
read from DEV_SENT), mock payments. Nothing here touches Supabase,
Razorpay or a real mailbox.
"""

import re
import sys
from datetime import timedelta
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))


@pytest.fixture()
def app_client(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'test.db').as_posix()}")
    monkeypatch.setenv("EMAIL_BACKEND", "console")
    monkeypatch.setenv("PAYMENTS_MODE", "mock")
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    from app_platform import config
    config.reset_settings_cache()
    import app_platform.config as cfg
    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path)
    import services.auth.email_sender as es
    monkeypatch.setattr(es, "DATA_DIR", tmp_path)
    es.DEV_SENT.clear()
    from middlewares import rate_limit
    rate_limit.reset_all()
    from fastapi.testclient import TestClient
    from wiring.app_runtime import create_app
    app = create_app()
    client = TestClient(app, headers={"X-Centra": "1"})
    yield client
    config.reset_settings_cache()


def last_code(email: str) -> str:
    import services.auth.email_sender as es
    for msg in reversed(es.DEV_SENT):
        if msg["to"] == email:
            m = re.search(r"\b(\d{6})\b", msg["body"])
            if m:
                return m.group(1)
    raise AssertionError(f"no code emailed to {email}")


def wizard_state(slug: str, plan: str = "Base", meta: str = "", staff: list | None = None,
                 name: str = "Test Salon", areas: list | None = None) -> dict:
    return {
        "business": {"category": "Beauty & Wellness", "type": "Salon", "name": name, "slug": slug,
                     "address": "1 Road", "city": "Hyderabad", "areas": areas or ["Madhapur"], "phone": "+91 9876543210", "whatsapp": "",
                     "days": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"], "open": "09:00", "close": "19:00"},
        "services": {"selected": ["Hair Trimming", "Manicure"], "showAll": False,
                     "showPricing": True, "prices": {"Hair Trimming": "500", "Manicure": "800"}, "custom": [],
                     "interval": "30", "customInterval": ""},
        "staff": staff if staff is not None else [],
        "plan": plan, "billing": "monthly", "metaVerified": meta,
    }


def signup_business(client, email: str, slug: str, **kw) -> None:  # kw -> wizard_state
    r = client.post("/api/signup/start", json={"name": "Owner", "email": email, "phone": "",
                                               "password": "Passw0rd!", "confirm": "Passw0rd!", "terms": True})
    assert r.status_code == 200, r.text
    r = client.post("/api/signup/verify", json={"email": email, "code": last_code(email)})
    assert r.status_code == 200, r.text
    r = client.post("/api/signup/checkout", json={"state": wizard_state(slug, **kw)})
    assert r.status_code == 200, r.text
    r = client.post("/api/payments/mock-pay", json={"order_id": r.json()["order"]["order_id"]})
    assert r.status_code == 200, r.text


def future_date(days: int = 3):
    from app_platform.timeutil import local_now
    from dbhandlers.database import utcnow
    return (local_now(utcnow()) + timedelta(days=days)).date()
