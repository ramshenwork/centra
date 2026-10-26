"""
services/auth/lockout.py
------------------------
The sign-in lockout ladder (RR's decisions, all of them):

- Round 1: 5 attempts. No counter shown until the last 2 tries
  ("2 attempts left" after the 3rd wrong try, "1 attempt left" after the 4th).
- 5th wrong try locks the account for 2 hours.
- Every later round: 2 attempts, then a lock twice as long as the last one
  (2h, 4h, 8h, 16h ...). No maximum.
- A successful sign-in resets everything back to round 1.
- A password reset does NOT clear the lock or the count.
- While locked, sign-in is refused without even checking the password.

The only cap here is technical: once the doubling would run past the
largest date Python can store, the lock is set to that date instead of
crashing. That is thousands of years out, so the "no maximum" rule holds.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

FIRST_ROUND_ATTEMPTS = 5
LATER_ROUND_ATTEMPTS = 2
BASE_LOCK_HOURS = 2
WARN_WHEN_REMAINING = 2
_FAR_FUTURE = datetime(9999, 1, 1)


@dataclass
class FailureOutcome:
    locked: bool
    locked_until: datetime | None
    attempts_left: int
    show_warning: bool


def attempts_allowed(lock_level: int) -> int:
    return FIRST_ROUND_ATTEMPTS if lock_level == 0 else LATER_ROUND_ATTEMPTS


def lock_hours(lock_level: int) -> int:
    """Lock length for the lock that ends round `lock_level` (0-based)."""
    return BASE_LOCK_HOURS * (2 ** lock_level)


def is_locked(user, now: datetime) -> bool:
    return bool(user.locked_until and user.locked_until > now)


def register_failure(user, now: datetime) -> FailureOutcome:
    allowed = attempts_allowed(user.lock_level)
    user.failed_attempts = (user.failed_attempts or 0) + 1
    if user.failed_attempts >= allowed:
        try:
            until = now + timedelta(hours=lock_hours(user.lock_level))
        except OverflowError:
            until = _FAR_FUTURE
        if until > _FAR_FUTURE:
            until = _FAR_FUTURE
        user.locked_until = until
        user.lock_level += 1
        user.failed_attempts = 0
        return FailureOutcome(True, until, 0, True)
    left = allowed - user.failed_attempts
    return FailureOutcome(False, None, left, left <= WARN_WHEN_REMAINING)


def register_success(user) -> None:
    user.failed_attempts = 0
    user.lock_level = 0
    user.locked_until = None
