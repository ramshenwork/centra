"""Pure rule tests: lockout ladder, slot engine, closures, Razorpay signature."""
from datetime import date, datetime, timedelta
from types import SimpleNamespace

from services.auth import lockout
from services.booking.availability import DayInput, StaffDay, compute_slots, period_of


def _user():
    return SimpleNamespace(failed_attempts=0, lock_level=0, locked_until=None)


def test_lockout_first_round_quiet_until_last_two():
    u, now = _user(), datetime(2026, 10, 25, 10)
    outs = [lockout.register_failure(u, now) for _ in range(4)]
    assert [o.show_warning for o in outs] == [False, False, True, True]
    assert [o.attempts_left for o in outs[2:]] == [2, 1]
    fifth = lockout.register_failure(u, now)
    assert fifth.locked and u.locked_until == now + timedelta(hours=2)


def test_lockout_doubles_with_two_attempts_and_resets_on_success():
    u, now = _user(), datetime(2026, 10, 25, 10)
    for _ in range(5):
        lockout.register_failure(u, now)
    for expected_hours in (4, 8, 16):
        later = u.locked_until + timedelta(minutes=1)
        first = lockout.register_failure(u, later)
        assert not first.locked and first.attempts_left == 1 and first.show_warning
        second = lockout.register_failure(u, later)
        assert second.locked and u.locked_until == later + timedelta(hours=expected_hours)
    lockout.register_success(u)
    assert (u.failed_attempts, u.lock_level, u.locked_until) == (0, 0, None)


def test_lockout_no_maximum_but_no_crash():
    u = _user()
    u.lock_level = 60
    u.failed_attempts = 1
    out = lockout.register_failure(u, datetime(2026, 1, 1))
    assert out.locked and u.locked_until.year == 9999


def _day(**kw):
    base = dict(open_min=9 * 60, close_min=19 * 60, interval=30, duration=30, staff=[], unstaffed_busy=[])
    base.update(kw)
    return DayInput(**base)


def test_service_interval_wins_and_staff_filters():
    ravi = StaffDay(1, "Ravi", 10 * 60, 13 * 60, 11 * 60, 12 * 60, True, busy=[(10 * 60 + 30, 11 * 60)])
    slots = [s["time"] for s in compute_slots(_day(staff=[ravi]), 1)]
    assert slots == ["10:00", "12:00", "12:30"]  # 10:30 busy, 11:00-11:30 break


def test_duration_must_fit_and_no_staff_single_chair():
    day = _day(duration=60, unstaffed_busy=[(9 * 60, 10 * 60)])
    times = [s["time"] for s in compute_slots(day)]
    assert "09:00" not in times and "09:30" not in times and times[0] == "10:00" and times[-1] == "18:00"


def test_today_hides_past_slots():
    times = [s["time"] for s in compute_slots(_day(earliest_start=13 * 60))]
    assert times[0] == "13:30"


def test_morning_ends_at_1130():
    assert period_of("11:30") == "Morning"
    assert period_of("12:00") == "Afternoon"
    assert period_of("13:00") == "Afternoon"
    assert period_of("16:00") == "Evening"


def test_closure_rules(app_client):
    from services.business.closures import rule_matches
    R = lambda **k: SimpleNamespace(**{"start_date": None, "end_date": None, "weekday": None, "day_of_month": None,
                                       "month": None, "festival_key": "", **k})
    assert rule_matches(R(type="range", start_date=date(2027, 3, 31), end_date=date(2028, 3, 31)), date(2028, 3, 31))
    assert rule_matches(R(type="weekly", weekday=1), date(2026, 10, 27))      # a Tuesday
    assert rule_matches(R(type="monthly", day_of_month=15), date(2026, 11, 15))
    assert rule_matches(R(type="yearly", month=8, day_of_month=15), date(2027, 8, 15))
    assert rule_matches(R(type="festival", festival_key="diwali"), date(2026, 11, 8))
    assert not rule_matches(R(type="festival", festival_key="diwali"), date(2026, 11, 9))


def test_checkout_signature(monkeypatch):
    import hashlib
    import hmac
    from app_platform import config
    from services.payments import gateway
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "s3cret")
    config.reset_settings_cache()
    sig = hmac.new(b"s3cret", b"order_1|pay_1", hashlib.sha256).hexdigest()
    assert gateway.verify_checkout_signature("order_1", "pay_1", sig)
    assert not gateway.verify_checkout_signature("order_1", "pay_2", sig)
    config.reset_settings_cache()
