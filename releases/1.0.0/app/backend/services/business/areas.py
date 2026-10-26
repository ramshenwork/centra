"""
services/business/areas.py
--------------------------
The localities a business lists so customers searching "near me in <area>"
(through AI assistants) find it.

- Area 1 is required: where the business actually is. It is shown when two
  businesses share a name ("Glow Salon, Madhapur or Kukatpally?").
- The others are optional nearby areas the owner wants to appear in.
- How many are allowed is limits.search_areas in shared/config/pricing.json
  (3 for now; raise it there, no code change).
"""
from __future__ import annotations

from app_platform.config import load_json_config
from app_platform.errors import bad_request


def areas_limit() -> int:
    return int(load_json_config("pricing").get("limits", {}).get("search_areas", 3))


def clean_areas(raw) -> list[str]:
    seen, out = set(), []
    for a in (raw or []):
        a = " ".join(str(a or "").split())[:60]
        if a and a.lower() not in seen:
            seen.add(a.lower())
            out.append(a)
    limit = areas_limit()
    if not out:
        raise bad_request("Enter the area / locality where your business is (e.g. Madhapur).", code="area_required")
    if len(out) > limit:
        raise bad_request(f"You can list up to {limit} areas.", code="too_many_areas")
    return out
