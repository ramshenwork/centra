"""
services/auth/email_sender.py
-----------------------------
All outgoing email goes through send_email(). Other services (booking,
notifications, payments) import from here; they never open SMTP
themselves.

EMAIL_BACKEND=console (development only) writes each message to
shared/data/dev-outbox/ so OTP codes can be read while testing. It is
refused in production because those files contain codes.
EMAIL_BACKEND=smtp sends through the SMTP server in shared/config/.env.
EMAIL_BACKEND=none sends nothing (only for testing with TEST_OTP_CODE,
when there is no SMTP account yet).

Subjects and bodies are never logged: OTP emails contain the code.
While TEST_OTP_CODE is set, a failed email never blocks sign-up or
password reset, because the fixed code works anyway.
"""
from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage
from email.utils import make_msgid

from app_platform.config import DATA_DIR, get_settings
from app_platform.errors import AppError
from app_platform.logger import get_logger, mask_email
from dbhandlers.database import utcnow

log = get_logger("email")
# Development/test only: the last messages sent with the console backend (tests read OTP codes here).
DEV_SENT: list[dict] = []


class EmailError(AppError):
    def __init__(self):
        super().__init__(502, "email_failed", "We couldn't send the email right now. Please try again in a minute.")


def send_email(to: str, subject: str, body: str, *, critical: bool = True) -> bool:
    """critical=True raises EmailError on failure (OTPs); False only logs (notifications)."""
    s = get_settings()
    try:
        if s.email_backend == "none":
            log.info("email skipped (EMAIL_BACKEND=none) to=%s", mask_email(to))
            return False
        if s.email_backend == "console":
            if s.is_production:
                raise RuntimeError("console email backend is not allowed in production")
            outbox = DATA_DIR / "dev-outbox"
            outbox.mkdir(parents=True, exist_ok=True)
            stamp = utcnow().strftime("%Y%m%dT%H%M%S%f")
            (outbox / f"{stamp}.txt").write_text(f"To: {to}\nSubject: {subject}\n\n{body}\n", encoding="utf-8")
            DEV_SENT.append({"to": to, "subject": subject, "body": body})
            del DEV_SENT[:-50]
        else:
            msg = EmailMessage()
            msg["From"] = s.smtp_from
            msg["To"] = to
            msg["Subject"] = subject
            msg["Message-ID"] = make_msgid(domain="centra")
            msg.set_content(body)
            with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=15) as smtp:
                if s.smtp_starttls:
                    smtp.starttls(context=ssl.create_default_context())
                if s.smtp_user:
                    smtp.login(s.smtp_user, s.smtp_password)
                smtp.send_message(msg)
        log.info("email sent to=%s", mask_email(to))
        return True
    except Exception as exc:  # noqa: BLE001
        log.error("email failed to=%s error=%s", mask_email(to), type(exc).__name__)
        if critical and not s.test_otp_code:
            raise EmailError() from exc
        return False


# ------------------------------------------------------------------ templates
def otp_email(code: str, purpose: str) -> tuple[str, str]:
    what = {
        "signup": "verify your email for CENTRA",
        "reset": "reset your CENTRA password",
        "manage_booking": "open your booking",
    }.get(purpose, "continue")
    subject = f"Your CENTRA code: {code}"
    body = (
        f"Use this code to {what}:\n\n    {code}\n\n"
        "It expires in 10 minutes and works once.\n"
        "If you didn't ask for this, you can ignore this email."
    )
    return subject, body


def password_changed_email() -> tuple[str, str]:
    s = get_settings()
    return (
        "Your CENTRA password was changed",
        "Your CENTRA password was just changed and all devices were signed out.\n\n"
        f"If this wasn't you, contact {s.support_email} straight away.",
    )
