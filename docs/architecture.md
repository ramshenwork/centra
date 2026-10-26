# CENTRA architecture (1.0.0)

**One app, many businesses (tenants).** Every business-owned row has a `business_id`. Dashboard requests resolve the business only from the signed-in owner's membership (`services/business/access.py::owner_ctx`), and any id from the browser goes through `owned()`, which answers "not found" for another business's rows.

**Request path:** browser → `SecurityMiddleware` (CSRF header `X-Centra: 1`, security headers, request log) → service router → service functions → SQLAlchemy → Supabase.

**Auth:** HttpOnly cookie session (random token; only its keyed hash is stored). Email OTPs are hashed, last 10 minutes, are single use, allow 5 wrong tries, and have a 60 s resend cooldown with at most 5 per hour. The lockout ladder lives in `services/auth/lockout.py`.

**Sign-up:**
- `PendingSignup` holds the owner's details until the OTP is verified.
- On verification, the User, a draft Business and the Membership are created.
- `signup_draft` JSON keeps wizard progress.
- Checkout validates and writes everything in one transaction, then creates a `Subscription` (pending) and a Razorpay order.
- The payment makes the business `active`.

**Booking engine** (`services/booking`):
- `availability.py` is pure slot maths: the service interval decides the start times, and staff shifts, breaks, leave, suspensions and existing bookings filter them.
- `engine.py` wraps it with the database: booking window, closures, holds (awaiting_payment + hold_expires_at, expired lazily), create (re-check under lock), cancel (refund or forfeit), reschedule, and manage tokens (an HMAC of a per-booking nonce, so the same link can be re-sent; it expires at the start time).

**Payments:** `gateway.py` is the only Razorpay caller. `service.mark_paid()` is idempotent and dispatches to subscription activation or deposit confirmation. Webhook events are de-duplicated by event id.

**Notifications:** `Notification` rows feed the dashboard bell over server-sent events (`/api/dashboard/events`, polled every 3 s server-side). Owner emails go out for new bookings and customer cancellations.

**Config as data:** prices, the service catalog and festival dates live in `shared/config/*.json`, so they change without a new release.

**Statuses:** awaiting_payment, requested, confirmed, completed, cancelled, rejected, expired, no_show, refunded, deposit_forfeited. Deposit statuses: not_required, pending, paid, refunded, forfeited, expired.
