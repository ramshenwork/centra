"""
wiring/app_runtime.py
---------------------
The ONE place the application is assembled: settings, logging, database,
middleware, every service's router, static files and error handling.
The HTTP entrypoint builds one of these and nothing else.

Database tables are created on startup if missing (create_all). This
release has no migration tool: adding a column later means running an
ALTER TABLE on Supabase before activating the new release.
"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app_platform.config import DATA_DIR, FRONTEND_DIR, get_settings
from app_platform.errors import AppError
from app_platform.logger import get_logger, setup_logging
from dbhandlers.database import create_all, init_engine
from middlewares.security import SecurityMiddleware

log = get_logger("runtime")

_FIELD_MESSAGES = {
    "email": "Enter a valid email address.", "date": "Choose a valid date.", "time": "Choose a valid time.",
    "services": "Choose at least one service.", "service_ids": "Choose at least one service.",
}


def create_app(database_url: str | None = None) -> FastAPI:
    setup_logging()
    s = get_settings()
    init_engine(database_url)
    create_all()

    app = FastAPI(title="CENTRA", version="1.0.0", docs_url=None if s.is_production else "/api/docs",
                  redoc_url=None, openapi_url=None if s.is_production else "/api/openapi.json")
    app.add_middleware(SecurityMiddleware)

    @app.exception_handler(AppError)
    async def app_error(_: Request, exc: AppError):
        return JSONResponse({"error": {"code": exc.code, "message": exc.message, **exc.extra}}, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError):
        msg = "Some details are missing or invalid. Please check the form."
        for err in exc.errors():
            loc = [str(x) for x in err.get("loc", [])]
            for field, text in _FIELD_MESSAGES.items():
                if field in loc:
                    msg = text
                    break
        return JSONResponse({"error": {"code": "invalid", "message": msg}}, status_code=400)

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception):
        log.exception("unhandled error path=%s", request.url.path)
        return JSONResponse({"error": {"code": "server_error",
                                       "message": "Something went wrong on our side. Please try again."}},
                            status_code=500)

    from services.auth.router import router as auth_router
    from services.booking.router import router as public_router
    from services.business.router import router as dashboard_router
    from services.payments.router import router as payments_router
    from services.signup.router import router as signup_router
    from wiring.pages import router as pages_router

    for r in (auth_router, signup_router, dashboard_router, public_router, payments_router):
        app.include_router(r)

    app.mount("/static", StaticFiles(directory=FRONTEND_DIR / "static"), name="static")
    uploads = DATA_DIR / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    app.mount("/uploads", StaticFiles(directory=uploads), name="uploads")
    app.include_router(pages_router)  # last: it owns the catch-all /{slug}
    if s.test_otp_code:
        log.warning("TESTING MODE: TEST_OTP_CODE is set. Every OTP is the fixed test code. "
                    "Remove TEST_OTP_CODE before real customers use the app.")
    log.info("CENTRA 1.0.0 ready env=%s payments=%s email=%s storage=%s",
             s.app_env, s.payments_mode, s.email_backend, s.storage_backend)
    return app