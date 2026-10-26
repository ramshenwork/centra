"""
dbhandlers/models.py
--------------------
Every table. Every business-owned row carries business_id, and every
query in the dashboard services filters on it.

Money is stored as integer paise (₹1 = 100 paise) so there is never a
rounding error. Datetimes are naive UTC. Business-facing clock times
(opening hours, staff shifts, breaks) are "HH:MM" strings in the
business's local time (Asia/Kolkata).
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    JSON, Boolean, Date, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from dbhandlers.database import Base, utcnow


# ---------------------------------------------------------------- identity
class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    phone: Mapped[str] = mapped_column(String(32), default="")
    password_hash: Mapped[str] = mapped_column(String(255))
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    # Lockout ladder state (services/auth/lockout.py)
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0)
    lock_level: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SessionToken(Base):
    __tablename__ = "sessions"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PendingSignup(Base):
    """Owner details held until the email OTP is verified; then a User is created."""
    __tablename__ = "pending_signups"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(254), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    phone: Mapped[str] = mapped_column(String(32), default="")
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class OtpCode(Base):
    __tablename__ = "otp_codes"
    id: Mapped[int] = mapped_column(primary_key=True)
    purpose: Mapped[str] = mapped_column(String(32))          # signup | reset | manage_booking
    target: Mapped[str] = mapped_column(String(254), index=True)  # lower-cased email
    ref: Mapped[str] = mapped_column(String(64), default="")  # e.g. booking code
    code_hash: Mapped[str] = mapped_column(String(64))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# ---------------------------------------------------------------- tenancy
class Business(Base):
    __tablename__ = "businesses"
    id: Mapped[int] = mapped_column(primary_key=True)
    owner_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    slug: Mapped[str | None] = mapped_column(String(60), unique=True, nullable=True)
    name: Mapped[str] = mapped_column(String(120), default="")
    category: Mapped[str] = mapped_column(String(60), default="")
    business_type: Mapped[str] = mapped_column(String(60), default="")
    address: Mapped[str] = mapped_column(String(255), default="")
    city: Mapped[str] = mapped_column(String(80), default="")
    phone: Mapped[str] = mapped_column(String(32), default="")
    whatsapp: Mapped[str] = mapped_column(String(32), default="")
    working_days: Mapped[list] = mapped_column(JSON, default=list)   # ["Mon", ...]
    open_time: Mapped[str] = mapped_column(String(5), default="09:00")
    close_time: Mapped[str] = mapped_column(String(5), default="19:00")
    show_pricing: Mapped[bool] = mapped_column(Boolean, default=True)
    allow_multiple: Mapped[bool] = mapped_column(Boolean, default=True)
    default_interval: Mapped[int] = mapped_column(Integer, default=30)
    logo_url: Mapped[str] = mapped_column(String(500), default="")
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft | active | suspended
    signup_draft: Mapped[dict] = mapped_column(JSON, default=dict)    # resume data for the wizard
    deposits_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    deposit_percent: Mapped[int] = mapped_column(Integer, default=10)
    refund_window_hours: Mapped[int] = mapped_column(Integer, default=24)
    plan: Mapped[str] = mapped_column(String(10), default="Base")       # Base | Top
    billing: Mapped[str] = mapped_column(String(10), default="monthly") # monthly | pack
    meta_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    plan_active_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    top_setup_status: Mapped[str] = mapped_column(String(20), default="")  # "" | pending | done
    linked_account_id: Mapped[str] = mapped_column(String(64), default="")  # Razorpay Route
    sound_alerts: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Membership(Base):
    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("user_id", "business_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(20), default="owner")  # owner only (no staff logins)


# ---------------------------------------------------------------- catalog
class Service(Base):
    __tablename__ = "services"
    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(String(255), default="")
    price_paise: Mapped[int] = mapped_column(Integer, default=0)
    duration_min: Mapped[int] = mapped_column(Integer, default=30)
    interval_min: Mapped[int] = mapped_column(Integer, default=30)
    shown: Mapped[bool] = mapped_column(Boolean, default=True)
    suspended_until: Mapped[date | None] = mapped_column(Date, nullable=True)
    suspended_forever: Mapped[bool] = mapped_column(Boolean, default=False)
    sort: Mapped[int] = mapped_column(Integer, default=0)


class Staff(Base):
    __tablename__ = "staff"
    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    role: Mapped[str] = mapped_column(String(80), default="")
    working_days: Mapped[list] = mapped_column(JSON, default=list)
    start_time: Mapped[str] = mapped_column(String(5), default="09:00")
    end_time: Mapped[str] = mapped_column(String(5), default="19:00")
    break_start: Mapped[str] = mapped_column(String(5), default="")
    break_end: Mapped[str] = mapped_column(String(5), default="")
    interval_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    shown: Mapped[bool] = mapped_column(Boolean, default=True)
    suspended_until: Mapped[date | None] = mapped_column(Date, nullable=True)
    suspended_forever: Mapped[bool] = mapped_column(Boolean, default=False)


class StaffLeave(Base):
    __tablename__ = "staff_leave"
    __table_args__ = (UniqueConstraint("staff_id", "leave_date"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    staff_id: Mapped[int] = mapped_column(ForeignKey("staff.id", ondelete="CASCADE"), index=True)
    leave_date: Mapped[date] = mapped_column(Date)
    reason: Mapped[str] = mapped_column(String(120), default="")


class Closure(Base):
    """
    type = range    -> start_date..end_date inclusive
           weekly   -> weekday (0=Mon .. 6=Sun)
           monthly  -> day_of_month (1..31)
           yearly   -> month + day_of_month (fixed calendar date every year)
           festival -> festival_key, dates looked up in shared/config/festivals.json
    """
    __tablename__ = "closures"
    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    type: Mapped[str] = mapped_column(String(10))
    label: Mapped[str] = mapped_column(String(80))
    note: Mapped[str] = mapped_column(String(255), default="")
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    weekday: Mapped[int | None] = mapped_column(Integer, nullable=True)
    day_of_month: Mapped[int | None] = mapped_column(Integer, nullable=True)
    month: Mapped[int | None] = mapped_column(Integer, nullable=True)
    festival_key: Mapped[str] = mapped_column(String(40), default="")


# ---------------------------------------------------------------- bookings
class Customer(Base):
    __tablename__ = "customers"
    __table_args__ = (UniqueConstraint("business_id", "email"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str] = mapped_column(String(254))
    phone: Mapped[str] = mapped_column(String(32))
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Booking(Base):
    __tablename__ = "bookings"
    __table_args__ = (
        Index("ix_bookings_business_start", "business_id", "start_at"),
        Index("ix_bookings_staff_start", "staff_id", "start_at"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    code: Mapped[str] = mapped_column(String(16), unique=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    staff_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id", ondelete="SET NULL"), nullable=True)
    start_at: Mapped[datetime] = mapped_column(DateTime)   # UTC
    end_at: Mapped[datetime] = mapped_column(DateTime)     # UTC
    local_date: Mapped[date] = mapped_column(Date, index=True)
    status: Mapped[str] = mapped_column(String(20), index=True)
    total_paise: Mapped[int] = mapped_column(Integer, default=0)
    deposit_paise: Mapped[int] = mapped_column(Integer, default=0)
    deposit_status: Mapped[str] = mapped_column(String(20), default="not_required")
    hold_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    refund_window_hours: Mapped[int] = mapped_column(Integer, default=24)  # snapshot at booking time
    note: Mapped[str] = mapped_column(String(500), default="")
    source: Mapped[str] = mapped_column(String(10), default="online")    # online | manual
    manage_nonce: Mapped[str] = mapped_column(String(32), default="")
    manage_token_hash: Mapped[str] = mapped_column(String(64), default="", index=True)
    manage_token_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reminder_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    items: Mapped[list["BookingItem"]] = relationship(cascade="all, delete-orphan", lazy="selectin")


class BookingItem(Base):
    """Snapshot of each service at booking time (later price edits don't change old bookings)."""
    __tablename__ = "booking_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    booking_id: Mapped[int] = mapped_column(ForeignKey("bookings.id", ondelete="CASCADE"), index=True)
    service_id: Mapped[int | None] = mapped_column(ForeignKey("services.id", ondelete="SET NULL"), nullable=True)
    name: Mapped[str] = mapped_column(String(120))
    price_paise: Mapped[int] = mapped_column(Integer, default=0)
    duration_min: Mapped[int] = mapped_column(Integer, default=30)


# ---------------------------------------------------------------- money
class Subscription(Base):
    __tablename__ = "subscriptions"
    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    owner_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    plan: Mapped[str] = mapped_column(String(10))
    billing: Mapped[str] = mapped_column(String(10))
    amount_paise: Mapped[int] = mapped_column(Integer)
    months: Mapped[int] = mapped_column(Integer)
    free_months: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(10), default="pending")  # pending | active | failed
    period_start: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    period_end: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Payment(Base):
    __tablename__ = "payments"
    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(15))   # deposit | subscription
    booking_id: Mapped[int | None] = mapped_column(ForeignKey("bookings.id", ondelete="SET NULL"), nullable=True)
    subscription_id: Mapped[int | None] = mapped_column(ForeignKey("subscriptions.id", ondelete="SET NULL"), nullable=True)
    order_id: Mapped[str] = mapped_column(String(64), unique=True)
    payment_id: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    amount_paise: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(12), default="created")  # created | paid | failed | refunded
    refund_id: Mapped[str] = mapped_column(String(64), default="")
    payout_status: Mapped[str] = mapped_column(String(20), default="")  # deposits: route_held | manual_pending | ""
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class WebhookEvent(Base):
    __tablename__ = "webhook_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[str] = mapped_column(String(80), unique=True)
    received_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# ---------------------------------------------------------------- notifications
class Notification(Base):
    __tablename__ = "notifications"
    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    type: Mapped[str] = mapped_column(String(20))   # Booking | Cancellation | System | Summary
    title: Mapped[str] = mapped_column(String(120))
    body: Mapped[str] = mapped_column(String(255), default="")
    unread: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
