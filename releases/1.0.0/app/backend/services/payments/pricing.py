"""
services/payments/pricing.py
----------------------------
Plan prices are NOT in code. They live in shared/config/pricing.json
(rupees) so RR can change them by editing one file and restarting.

The server always works out the amount itself; the price the browser
shows is only for display.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app_platform.config import load_json_config
from app_platform.errors import bad_request
from dbhandlers.models import Subscription

PLANS = ("Base", "Top")
BILLINGS = ("monthly", "pack")


@dataclass
class Quote:
    plan: str
    billing: str
    amount_paise: int
    months: int
    free_months: int
    label: str


def config() -> dict:
    return load_json_config("pricing")


def is_first_time_top(db: Session, owner_user_id: int) -> bool:
    prior = db.scalars(select(Subscription.id).where(
        Subscription.owner_user_id == owner_user_id, Subscription.plan == "Top", Subscription.status == "active",
    )).first()
    return prior is None


def quote(db: Session, owner_user_id: int, plan: str, billing: str) -> Quote:
    if plan not in PLANS or billing not in BILLINGS:
        raise bad_request("Choose a valid plan and billing option.")
    cfg = config()
    entry = cfg["plans"][plan][billing]
    free = 0
    if plan == "Top" and is_first_time_top(db, owner_user_id):
        free = int(cfg.get("first_time_top_free_months", {}).get(billing, 0))
    return Quote(plan, billing, int(round(float(entry["amount"]) * 100)), int(entry["months"]), free,
                 cfg.get("offer_label", ""))


def public_table(first_time_top: bool = True) -> dict:
    cfg = config()
    out = {"offer_label": cfg.get("offer_label", ""), "currency": cfg.get("currency", "INR"), "plans": {}}
    for plan in PLANS:
        out["plans"][plan] = {}
        for billing in BILLINGS:
            e = cfg["plans"][plan][billing]
            free = int(cfg.get("first_time_top_free_months", {}).get(billing, 0)) if (plan == "Top" and first_time_top) else 0
            out["plans"][plan][billing] = {"amount": e["amount"], "months": e["months"], "free_months": free}
    return out
