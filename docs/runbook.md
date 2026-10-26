# CENTRA runbook

## First-time production setup

1. **Supabase**
   - Create a project (region: Mumbai).
   - Copy the connection string from Project settings > Database > Connection string (pooler, port 6543). Use it as `DATABASE_URL`, prefixed `postgresql+psycopg://`.
   - Create a **public** Storage bucket called `logos`. Copy the project URL and the `service_role` key into `SUPABASE_URL` / `SUPABASE_SERVICE_KEY`.
   - Tables are created automatically on the first start.
2. **Email:** any SMTP provider (Gmail app password, Zoho, Brevo, SES…). Fill `SMTP_*`. OTPs fail without it.
3. **Razorpay**
   - Use test keys first, then live keys: `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`.
   - Dashboard > Webhooks: URL `https://<app>/api/payments/webhook`, events `payment.captured`, `order.paid`, `payment.failed`, and a secret, which becomes `RAZORPAY_WEBHOOK_SECRET`.
   - Leave `ROUTE_ENABLED=false` until Razorpay approves Route.
4. **Render** (web service, Starter plan)
   - Build command: `pip install -r releases/1.0.0/app/backend/requirements.txt`
   - Start command: `python bin/launcher.py http`
   - Health check path: `/ready`
   - Environment: everything in `shared/config/.env.example`, with `APP_ENV=production`, `APP_BASE_URL=https://<your-app>.onrender.com`, `STORAGE_BACKEND=supabase`.
5. **Reminders (optional):** a Render cron job, hourly:
   `curl -X POST -H "X-Cron-Secret: $CRON_SECRET" https://<app>/api/internal/send-reminders`

## Testing mode

Set `TEST_OTP_CODE=454545` in Render to make every OTP that fixed code, and `EMAIL_BACKEND=none` if SMTP isn't set up yet. Remove `TEST_OTP_CODE` before launch.

## Everyday

- **Health:** `GET /health` (process up) and `GET /ready` (database reachable + pages present).
- **Logs:** Render's log tab and `shared/logs/app/app.log`. Lines never contain passwords, codes, tokens or full phone numbers.
- **Data:** Supabase Table Editor. Use a read-only role for browsing.
- **Change prices:** edit `shared/config/pricing.json`, then restart.
- **Add or rename services in the sign-up lists:** edit `shared/config/catalog.json`, then restart. This only affects new sign-ups.
- **Festival dates:** add each year's date to `shared/config/festivals.json` before it's needed. The dashboard warns owners when a date is missing.
- **Top customer paid:** you get an email at `ALERT_EMAIL`. Call them, set up Twilio, then set `top_setup_status` to `done` for that business in Supabase.

## New release (your own machine, Windows)

```
bin\deploy.ps1 -Version <new> -SourcePath <path\to\new\app>
bin\approve-release.ps1 -Version <new>     # runs tests/preflight; must pass
bin\activate-release.ps1 -Version <new>    # flips state\active-release.json
```
Restart the launcher. Roll back with `bin\rollback.ps1`.

On Render, deploys come from git: commit the new `releases/<version>/` folder and the updated `state/active-release.json` together. The build command must then point at the new release's `requirements.txt`. Render wipes local files on each deploy, which is why logos must use Supabase Storage there.

Version numbers are decided by RR only.