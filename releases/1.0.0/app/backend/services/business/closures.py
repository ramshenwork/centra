"""
services/business/closures.py
-----------------------------
Decides whether a business is closed on a date because of a closure rule.

Rule types (owner sets them in the dashboard "Business closures" section):
- range:    one-off period, start and end both included
- weekly:   every <weekday>
- monthly:  every <day> of the month (a 31st rule simply skips short months)
- yearly:   same calendar date every year (e.g. 15 Aug)
- festival: a festival whose date moves each year (e.g. Diwali). Dates
            come from shared/config/festivals.json, which RR keeps up to
            date. A festival with no date listed for a year is treated as
            open that year, and the dashboard flags it.

Normal working days and hours are handled in the availability engine;
closure rules are checked on top and always win.
"""
from __future__ import annotations

from datetime import date

from app_platform.config import load_json_config


def festivals() -> dict:
    return load_json_config("festivals").get("festivals", {})


def festival_date(key: str, year: int) -> date | None:
    fest = festivals().get(key)
    if not fest:
        return None
    value = fest.get("dates", {}).get(str(year))
    return date.fromisoformat(value) if value else None


def rule_matches(rule, d: date) -> bool:
    t = rule.type
    if t == "range":
        return bool(rule.start_date and rule.end_date and rule.start_date <= d <= rule.end_date)
    if t == "weekly":
        return rule.weekday is not None and d.weekday() == rule.weekday
    if t == "monthly":
        return rule.day_of_month is not None and d.day == rule.day_of_month
    if t == "yearly":
        return rule.month is not None and rule.day_of_month is not None and (d.month, d.day) == (rule.month, rule.day_of_month)
    if t == "festival":
        return festival_date(rule.festival_key, d.year) == d
    return False


def closed_by_rules(rules, d: date) -> bool:
    return any(rule_matches(r, d) for r in rules)


def missing_festival_years(rules, years: list[int]) -> list[str]:
    """Festival rules with no date configured for one of `years` (shown as a warning)."""
    out = []
    for r in rules:
        if r.type == "festival":
            for y in years:
                if festival_date(r.festival_key, y) is None:
                    label = festivals().get(r.festival_key, {}).get("label", r.festival_key)
                    out.append(f"{label} {y}")
    return out
