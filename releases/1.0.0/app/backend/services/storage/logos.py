"""
services/storage/logos.py
-------------------------
Owner logo uploads. PNG only (checked by file signature, not by the file
name), at most 1 MB.

STORAGE_BACKEND=local     saves under shared/data/uploads/logos (served at /uploads/...).
                          Fine on your own machine. On Render the disk is
                          wiped on each deploy, so use supabase there.
STORAGE_BACKEND=supabase  uploads to the Supabase Storage bucket
                          SUPABASE_BUCKET (create it as a PUBLIC bucket).

SUPABASE_SERVICE_KEY can be either kind of Supabase key:
- new secret key "sb_secret_...": sent ONLY in the apikey header. Supabase
  rejects it with "Invalid JWT" if it is also sent as Authorization: Bearer.
- legacy service_role key "eyJ...": sent in both apikey and Authorization.
"""
from __future__ import annotations

import secrets

import httpx

from app_platform.config import DATA_DIR, get_settings
from app_platform.errors import AppError, bad_request
from app_platform.logger import get_logger

log = get_logger("storage")
MAX_BYTES = 1_000_000
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def validate_png(data: bytes) -> None:
    if not data:
        raise bad_request("Choose a PNG file to upload.")
    if len(data) > MAX_BYTES:
        raise bad_request("The logo must be smaller than 1 MB.")
    if not data.startswith(PNG_MAGIC) or data[12:16] != b"IHDR":
        raise bad_request("PNG files only.")


def supabase_headers(key: str) -> dict:
    headers = {"apikey": key}
    if not key.startswith("sb_"):
        headers["Authorization"] = f"Bearer {key}"  # legacy JWT keys only
    return headers


def save_logo(business_id: int, data: bytes) -> str:
    validate_png(data)
    s = get_settings()
    name = f"{business_id}-{secrets.token_hex(6)}.png"
    if s.storage_backend == "supabase":
        if not (s.supabase_url and s.supabase_service_key):
            raise AppError(500, "storage_config", "Logo storage isn't configured yet.")
        url = f"{s.supabase_url}/storage/v1/object/{s.supabase_bucket}/logos/{name}"
        try:
            r = httpx.post(url, content=data, timeout=20, headers={
                **supabase_headers(s.supabase_service_key),
                "Content-Type": "image/png", "x-upsert": "true",
            })
        except httpx.HTTPError as exc:
            raise AppError(502, "storage_failed", "The logo couldn't be uploaded. Please try again.") from exc
        if r.status_code >= 300:
            log.error("supabase upload failed status=%s", r.status_code)
            raise AppError(502, "storage_failed", "The logo couldn't be uploaded. Please try again.")
        return f"{s.supabase_url}/storage/v1/object/public/{s.supabase_bucket}/logos/{name}"
    folder = DATA_DIR / "uploads" / "logos"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_bytes(data)
    return f"/uploads/logos/{name}"
