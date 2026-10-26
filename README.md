# CENTRA 1.0.0

Booking and business-management web app for Indian service businesses
(salons, spas, dental clinics, car detailing and servicing, photography
studios). One shared app serves every business. Each business gets a
public booking page at `/<slug>` and a private dashboard at `/dashboard`.

**Stack:** Python 3.12 · FastAPI · SQLAlchemy · Supabase Postgres (SQLite
for development) · plain HTML/JS pages · Razorpay · SMTP email · Render
hosting.

**Parked in this release:** the Top plan's WhatsApp receptionist (only
the plan, the Meta-verified check, the payment and the alert email to
RR exist), phone OTP, and Razorpay Route (code is present but off).

---

## 1. How it works, in plain words

1. **Owner signs up** (`/signup`). They enter their name, email,
   optional phone and password. A 6-digit code is emailed, and nothing
   is saved until that code is entered. Then the wizard collects
   business details, services (ticked from grouped lists), optional
   staff, and plan. Progress is saved on the server after every step.
2. **Owner pays.** At "Review & pay" the server re-checks everything,
   saves business, services and staff in one go, and opens Razorpay.
   The booking page goes live only when the server confirms the
   payment with Razorpay, never because the browser says so.
3. **Customers book** at `/<slug>`. They pick services, then staff (or
   "Any available"), then a date in the next 2 months, then a free time.
   They enter name, email and phone; no account is needed. If the
   business takes deposits, the slot is held for 10 minutes while they
   pay.
4. **Customers manage** through a secret link (`/manage?token=…`) that
   works only for that booking and stops at the appointment time. A
   lost link is recovered with the booking code and an email code.
5. **Owner runs the business** from `/dashboard`: appointments,
   services, staff, closures, customers, deposits, reports and
   subscription. New bookings and cancellations appear instantly (and
   are emailed).

## 2. Run it locally

```bash
cd releases/1.0.0/app/backend
pip install -r requirements.txt
cd ../../../..                       # project root
python bin/launcher.py http          # http://localhost:8000
```

With no `shared/config/.env`, it runs in development mode: SQLite at
`shared/data/centra.db`, mock payments (a confirm box instead of
Razorpay), and emails written to `shared/data/dev-outbox/` (read your
OTP codes there).

Tests (also the release gate): `cd releases/1.0.0/app/backend && python -m pytest tests/preflight`

## 3. Deploy on Render with Supabase

See `docs/runbook.md`. In short:
- **Build command:** `pip install -r releases/1.0.0/app/backend/requirements.txt`
- **Start command:** `python bin/launcher.py http`
- **Settings:** set the environment variables from
  `shared/config/.env.example` in Render (APP_ENV=production,
  SECRET_KEY, DATABASE_URL, SMTP_*, RAZORPAY_*,
  STORAGE_BACKEND=supabase, SUPABASE_*).
- **Razorpay webhook:** point it at `https://<app>/api/payments/webhook`
  with events `payment.captured`, `order.paid` and `payment.failed`.

## 4. Folder structure

```
bin/            launcher.py (never changes; runs the active release) + deploy/approve/activate/rollback .ps1
state/          active-release.json: which releases/<version>/ is live (changing it = deploy / rollback)
shared/config/  .env (secrets, never committed), pricing.json, catalog.json, festivals.json
shared/data/    SQLite (dev), uploads (dev), dev-outbox (dev emails)
shared/logs/    app.log
docs/           runbook.md, architecture.md
releases/1.0.0/
  release.json, deployment-status.json
  app/frontend/pages/     signin, signup, forgot, management, booking (also /manage), terms, notfound
  app/frontend/static/    scripts/api.js (API + Razorpay helper), styles/centra-auth.css
  app/backend/
    entrypoints/          http_entrypoint.py
    wiring/               app_runtime.py (assembles everything), pages.py (URL -> page)
    app_platform/         config, errors, logger, timeutil
    dbhandlers/           database.py (engine), models.py (all tables)
    middlewares/          security.py (CSRF, headers, request log), rate_limit.py
    services/auth/        sign-in, lockout ladder, OTP, sessions, forgot password, ALL email sending
    services/signup/      wizard validation + writes the business to the database
    services/business/    dashboard routes, dashboard state, closures, tenant access control
    services/booking/     slot engine, bookings, holds, manage links, public booking routes
    services/payments/    Razorpay gateway, plan pricing, payment confirmation, webhook
    services/notifications/  dashboard bell + owner emails + Top alert to RR
    services/reports/     reports and payment summaries
    services/storage/     logo PNG upload (local or Supabase Storage)
    tests/preflight/      18 tests: rules + full API flows
```

## 5. Rules built in (RR's decisions)

- **Sign-in lockout:** 5 tries, with the counter shown only for the
  last 2. Then a 2-hour lock, then 2 tries per round with locks
  doubling (4h, 8h, 16h…) and no maximum. A successful sign-in resets
  everything. A password reset does not clear a lock. The lock message
  shows SUPPORT_EMAIL.
- **Forgot password:** email OTP, and the page says when no account
  exists.
- **Password:** at least 8 characters, with letters, numbers and one
  special character.
- **Slots:** the service's interval decides the start times, and staff
  hours, breaks, leave and existing bookings filter them. Morning runs
  until 11:30, Afternoon until 15:59, then Evening.
- **Booking window:** a rolling 2 months. Closed days (working days and
  closure rules) can't be selected.
- **Closures:** date range, weekly, monthly, yearly, or festival
  (dates in `festivals.json`). A business closing on a booked day
  contacts its customers itself (stated in T&C).
- **Deposits:** each business sets a percentage and a refund window.
  Cancelling before the window refunds the deposit, later forfeits it.
  An owner cancelling always refunds.
- **Plans:** prices come from `shared/config/pricing.json` (Base
  ₹479/month or ₹1,689 for 6 months; Top ₹889/month or ₹2,169 for
  6 months), shown with the "30% off · starting offer" label. First-time
  Top buyers get 1 extra month.
- **Top plan:** needs a Meta-verified business. Answering "No" switches
  to Base with no Top charge. A paid Top plan emails ALERT_EMAIL (RR)
  with the owner's details for manual Twilio setup.
- **Contact details:** owner phone is optional and not verified.
  Customer email and phone are required, and the phone isn't verified.
- **Staff:** no staff logins.
- **Logo:** PNG only, under 1 MB.

## 6. Assumptions made during the build (confirm or change)

1. New services start with **duration = slot interval**, since the
   wizard doesn't ask for duration. Owners edit it in Services.
2. With several services picked, the **smallest interval** sets the
   start times and the **durations add up**.
3. A business with **no staff takes one booking at a time**.
4. Online bookings **without a deposit are confirmed straight away**.
5. "Suspend for N days" **counts today as day 1**.
6. Manual (owner) bookings skip the slot check, so the owner can
   squeeze in walk-ins. Owner reschedules are checked for clashes.
7. Duplicate removed from the catalog: "Corporate Video Production"
   was listed twice under Photography.
8. The **Diwali dates** in `festivals.json` must be verified.

## 7. Check these first (most vulnerable logic)

1. **Payment confirmation** (`services/payments/service.py`): activation
   happens only after the server verifies the signature and fetches the
   payment from Razorpay, or after a signed webhook. Test with real
   Razorpay test keys before launch, because mock mode skips both.
2. **Double booking** (`services/booking/engine.py::create_booking`):
   the slot is re-checked under a per-business lock (row lock on
   Postgres). Test two bookings for the same slot at the same moment
   on Supabase.
3. **Late deposit payment** (`confirm_deposit`): a payment that arrives
   after the 10-minute hold either confirms (slot still free) or is
   auto-refunded.
4. **Tenant isolation** (`services/business/access.py`): the business is
   always taken from the session, and `owned()` guards every id. A test
   covers this, but re-check any new route you add.
5. **Slot maths** (`services/booking/availability.py`): breaks,
   multi-service durations, today's cut-off, and time-zone conversion
   (bookings are stored in UTC and shown in Asia/Kolkata).
6. **Rate limits** are in-memory: they reset on restart and aren't
   shared if Render runs more than one instance.
7. **No database migrations:** tables are created on first start.
   Adding a column later needs a manual `ALTER TABLE` on Supabase
   before activating the new release.
8. **Razorpay Route** is untested against the live API and stays off
   (`ROUTE_ENABLED=false`) until Razorpay approves the account under
   the RBI payment aggregator rules.
9. **Holding customers' deposits** in the platform's Razorpay account
   has regulatory weight. Confirm with a CA / Razorpay.
