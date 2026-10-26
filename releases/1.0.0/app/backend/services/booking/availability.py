"""
services/booking/availability.py
--------------------------------
Pure slot maths. No database here, so every rule can be tested directly.

How a day's slots are worked out (all in the business's local time):

1. Candidate start times step by the SERVICE interval (RR's rule B:
   "the service's interval wins"). With several services picked, the
   smallest of their intervals is used, and the booking length is the sum
   of their durations.
2. A candidate must fit entirely inside business opening hours, and on
   today's date it must start after the current time.
3. If the business has no staff, there is one chair: a time is free when
   no other booking overlaps it.
4. If it has staff, a staff member can take a time when it is one of their
   working days, the whole booking fits inside their shift, it doesn't
   touch their break, they're not on leave or suspended, and none of
   their bookings overlap. "Any available" means at least one staff
   member can take it.

Closed days (working days and closure rules) are decided before this is
called; a closed day simply gets no slots.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app_platform.timeutil import from_minutes, label_12h, to_minutes


@dataclass
class StaffDay:
    id: int
    name: str
    start: int            # minutes from midnight
    end: int
    break_start: int | None
    break_end: int | None
    available_today: bool  # works this weekday, not on leave, not suspended
    busy: list[tuple[int, int]] = field(default_factory=list)


@dataclass
class DayInput:
    open_min: int
    close_min: int
    interval: int
    duration: int
    staff: list[StaffDay]           # empty list = business has no staff
    unstaffed_busy: list[tuple[int, int]]
    earliest_start: int | None = None  # set on "today": slots must start after this


def _overlaps(a0: int, a1: int, b0: int, b1: int) -> bool:
    return a0 < b1 and b0 < a1


def staff_can_take(st: StaffDay, start: int, end: int) -> bool:
    if not st.available_today:
        return False
    if start < st.start or end > st.end:
        return False
    if st.break_start is not None and st.break_end is not None and _overlaps(start, end, st.break_start, st.break_end):
        return False
    return not any(_overlaps(start, end, b0, b1) for b0, b1 in st.busy)


def candidate_starts(day: DayInput) -> list[int]:
    if day.interval <= 0 or day.duration <= 0:
        return []
    out = []
    t = day.open_min
    while t + day.duration <= day.close_min:
        if day.earliest_start is None or t > day.earliest_start:
            out.append(t)
        t += day.interval
    return out


def compute_slots(day: DayInput, staff_choice: str | int = "any") -> list[dict]:
    """Returns only bookable slots: [{"time","label","period","staff_ids"}]."""
    slots = []
    for t in candidate_starts(day):
        end = t + day.duration
        if not day.staff:
            if any(_overlaps(t, end, b0, b1) for b0, b1 in day.unstaffed_busy):
                continue
            free = []
        else:
            free = [st.id for st in day.staff if staff_can_take(st, t, end)]
            if staff_choice == "any":
                if not free:
                    continue
            elif int(staff_choice) not in free:
                continue
        hhmm = from_minutes(t)
        slots.append({"time": hhmm, "label": label_12h(hhmm), "period": period_of(hhmm), "staff_ids": free})
    return slots


def period_of(hhmm: str) -> str:
    """Morning ends at 11:59 (so 11:30 is the last morning slot on a 30-min interval)."""
    m = to_minutes(hhmm)
    if m < 12 * 60:
        return "Morning"
    if m < 16 * 60:
        return "Afternoon"
    return "Evening"


def pick_staff(day: DayInput, start: int, staff_choice: str | int) -> int | None:
    """Which staff member gets the booking. 'any' = first free one by id."""
    end = start + day.duration
    if not day.staff:
        return None
    free = [st.id for st in sorted(day.staff, key=lambda s: s.id) if staff_can_take(st, start, end)]
    if staff_choice == "any":
        return free[0] if free else -1
    return int(staff_choice) if int(staff_choice) in free else -1


def interval_and_duration(services, default_interval: int) -> tuple[int, int]:
    intervals = [s.interval_min for s in services if s.interval_min]
    interval = min(intervals) if intervals else default_interval
    duration = sum(max(5, s.duration_min) for s in services)
    return interval, duration


def to_min(hhmm: str | None) -> int | None:
    return to_minutes(hhmm) if hhmm else None
