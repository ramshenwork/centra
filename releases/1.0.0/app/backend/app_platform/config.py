"""
app_platform/config.py
----------------------
One place that knows where things live and what every setting is.

Settings come from environment variables first, then from
shared/config/.env (a plain KEY=VALUE file). Business-editable data that
RR changes by hand (plan prices, the preloaded service catalog, festival
dates) lives in JSON files under shared/config/ so that changing a price
never needs a code change or a new release.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent          # .../app/backend
APP_DIR = BACKEND_DIR.parent                                   # .../app
RELEASE_DIR = APP_DIR.parent                                   # .../releases/1.0.0
PROJECT_ROOT = RELEASE_DIR.parent.parent                       # project root
FRONTEND_DIR = APP_DIR / "frontend"
SHARED_DIR = PROJECT_ROOT / "shared"
CONFIG_DIR = SHARED_DIR / "config"
DATA_DIR = SHARED_DIR / "data"
LOG_DIR = SHARED_DIR / "logs" / "app"


def _load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        values[key.strip()] = value
    return values


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    app_env: str
    base_url: str
    public_domain_label: str
    database_url: str
    secret_key: str
    session_hours: int
    cookie_secure: bool
    support_email: str
    alert_email: str
    email_backend: str
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    smtp_from: str
    smtp_starttls: bool
    payments_mode: str
    razorpay_key_id: str
    razorpay_key_secret: str
    razorpay_webhook_secret: str
    route_enabled: bool
    storage_backend: str
    supabase_url: str
    supabase_service_key: str
    supabase_bucket: str
    cron_secret: str
    booking_hold_minutes: int
    booking_window_months: int
    timezone: str
    test_otp_code: str
    extra: dict = field(default_factory=dict)

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


def _build_settings(env: dict[str, str]) -> Settings:
    def get(key: str, default: str = "") -> str:
        return os.environ.get(key, env.get(key, default))

    app_env = get("APP_ENV", "development").lower()
    is_prod = app_env == "production"
    secret = get("SECRET_KEY", "")
    if not secret:
        if is_prod:
            raise RuntimeError("SECRET_KEY must be set in production (shared/config/.env).")
        secret = "dev-only-secret-change-me"
    payments_mode = get("PAYMENTS_MODE", "razorpay" if is_prod else "mock").lower()
    if is_prod and payments_mode == "mock":
        raise RuntimeError("PAYMENTS_MODE=mock is not allowed in production.")
    test_otp = get("TEST_OTP_CODE", "").strip()
    if test_otp and not (len(test_otp) == 6 and test_otp.isdigit()):
        raise RuntimeError("TEST_OTP_CODE must be exactly 6 digits (or removed).")
    default_db = "sqlite:///" + str((DATA_DIR / "centra.db").as_posix())
    return Settings(
        app_env=app_env,
        base_url=get("APP_BASE_URL", "http://localhost:8000").rstrip("/"),
        public_domain_label=get("PUBLIC_DOMAIN_LABEL", "centra.in"),
        database_url=get("DATABASE_URL", default_db),
        secret_key=secret,
        session_hours=int(get("SESSION_HOURS", "12")),
        cookie_secure=_bool(get("COOKIE_SECURE"), is_prod),
        support_email=get("SUPPORT_EMAIL", "ramshenwork@gmail.com"),
        alert_email=get("ALERT_EMAIL", "ramshenwork@gmail.com"),
        email_backend=get("EMAIL_BACKEND", "smtp" if is_prod else "console").lower(),
        smtp_host=get("SMTP_HOST"),
        smtp_port=int(get("SMTP_PORT", "587")),
        smtp_user=get("SMTP_USER"),
        smtp_password=get("SMTP_PASSWORD"),
        smtp_from=get("SMTP_FROM", "CENTRA <no-reply@centra.in>"),
        smtp_starttls=_bool(get("SMTP_STARTTLS"), True),
        payments_mode=payments_mode,
        razorpay_key_id=get("RAZORPAY_KEY_ID"),
        razorpay_key_secret=get("RAZORPAY_KEY_SECRET"),
        razorpay_webhook_secret=get("RAZORPAY_WEBHOOK_SECRET"),
        route_enabled=_bool(get("ROUTE_ENABLED"), False),
        storage_backend=get("STORAGE_BACKEND", "local").lower(),
        supabase_url=get("SUPABASE_URL").rstrip("/"),
        supabase_service_key=get("SUPABASE_SERVICE_KEY"),
        supabase_bucket=get("SUPABASE_BUCKET", "logos"),
        cron_secret=get("CRON_SECRET"),
        booking_hold_minutes=int(get("BOOKING_HOLD_MINUTES", "10")),
        booking_window_months=int(get("BOOKING_WINDOW_MONTHS", "2")),
        timezone=get("TIMEZONE", "Asia/Kolkata"),
        test_otp_code=test_otp,
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return _build_settings(_load_env_file(CONFIG_DIR / ".env"))


def reset_settings_cache() -> None:
    """Used by tests after changing environment variables."""
    get_settings.cache_clear()
    load_json_config.cache_clear()


@lru_cache(maxsize=8)
def load_json_config(name: str) -> dict:
    """Reads shared/config/<name>.json. Edited by hand; restart to apply."""
    path = CONFIG_DIR / f"{name}.json"
    if not path.exists():
        raise RuntimeError(f"Missing config file: {path}")
    return json.loads(path.read_text(encoding="utf-8"))