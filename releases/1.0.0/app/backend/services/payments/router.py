"""
services/payments/router.py
---------------------------
POST /api/payments/verify      browser hands back Razorpay checkout result (verified server-side)
POST /api/payments/webhook     Razorpay server-to-server events (signature checked)
POST /api/payments/mock-pay    development only: completes a mock order
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app_platform.config import get_settings
from app_platform.errors import AppError, not_found
from dbhandlers.database import get_db, utcnow
from middlewares.rate_limit import limit_ip
from services.payments import gateway
from services.payments.service import handle_webhook, mark_paid, verify_and_mark

router = APIRouter(prefix="/api/payments", tags=["payments"])


class VerifyIn(BaseModel):
    order_id: str
    payment_id: str
    signature: str


class MockIn(BaseModel):
    order_id: str


@router.post("/verify")
def verify(body: VerifyIn, request: Request, db: Session = Depends(get_db)):
    limit_ip(request, "pay-verify", 30, 300)
    result = verify_and_mark(db, body.order_id, body.payment_id, body.signature, utcnow())
    db.commit()
    return {"ok": True, **result}


@router.post("/webhook")
async def webhook(request: Request, db: Session = Depends(get_db)):
    raw = await request.body()
    if not gateway.verify_webhook_signature(raw, request.headers.get("x-razorpay-signature", "")):
        raise AppError(400, "bad_signature", "Invalid signature.")
    try:
        event = json.loads(raw)
    except json.JSONDecodeError:
        raise AppError(400, "bad_json", "Invalid body.")
    result = handle_webhook(db, event, request.headers.get("x-razorpay-event-id", ""), utcnow())
    db.commit()
    return result


@router.post("/mock-pay")
def mock_pay(body: MockIn, db: Session = Depends(get_db)):
    s = get_settings()
    if s.is_production or not gateway.is_mock() or not body.order_id.startswith("order_mock_"):
        raise not_found()
    result = mark_paid(db, body.order_id, "pay_mock_" + body.order_id[-8:], utcnow())
    db.commit()
    return {"ok": True, **result}
