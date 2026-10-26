"""
wiring/pages.py
---------------
Serves the HTML pages. URL map:

/                   sign-in (landing page)
/signup             business sign-up wizard
/forgot-password    password reset by email OTP
/dashboard          business management (signed-in owners only)
/manage?token=...   customer's booking management link
/terms              terms and conditions
/<slug>             a business's public booking page
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app_platform.config import FRONTEND_DIR
from dbhandlers.database import get_db
from dbhandlers.models import Business, User
from services.auth.sessions import optional_user
from services.business.access import business_for_user

router = APIRouter(include_in_schema=False)
PAGES = FRONTEND_DIR / "pages"
NO_CACHE = {"Cache-Control": "no-cache"}


def page(name: str, status: int = 200) -> FileResponse:
    return FileResponse(PAGES / name, status_code=status, headers=NO_CACHE, media_type="text/html")


@router.get("/health")
def health():
    return {"ok": True}


@router.get("/ready")
def ready(db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001
        return JSONResponse({"ready": False, "database": False}, status_code=503)
    missing = [p for p in ("signin.html", "signup.html", "management.html", "booking.html") if not (PAGES / p).exists()]
    if missing:
        return JSONResponse({"ready": False, "missing_pages": missing}, status_code=503)
    return {"ready": True, "database": True}


@router.get("/")
def signin_page(db: Session = Depends(get_db), user: User | None = Depends(optional_user)):
    if user is not None:
        biz = business_for_user(db, user)
        return RedirectResponse("/dashboard" if biz and biz.status != "draft" else "/signup", status_code=303)
    return page("signin.html")


@router.get("/signup")
def signup_page():
    return page("signup.html")


@router.get("/forgot-password")
def forgot_page():
    return page("forgot.html")


@router.get("/terms")
def terms_page():
    return page("terms.html")


@router.get("/dashboard")
def dashboard_page(db: Session = Depends(get_db), user: User | None = Depends(optional_user)):
    if user is None:
        return RedirectResponse("/", status_code=303)
    biz = business_for_user(db, user)
    if biz is None or biz.status == "draft":
        return RedirectResponse("/signup", status_code=303)
    return page("management.html")


@router.get("/manage")
def manage_page():
    return page("booking.html")


@router.get("/favicon.ico")
def favicon():
    return HTMLResponse("", status_code=204)


@router.get("/{slug}")
def booking_page(slug: str, db: Session = Depends(get_db)):
    biz = db.scalars(select(Business).where(Business.slug == slug.lower())).first()
    if biz is None or biz.status != "active":
        return page("notfound.html", 404)
    return page("booking.html")
