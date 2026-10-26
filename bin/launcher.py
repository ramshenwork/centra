#!/usr/bin/env python3
"""
bin/launcher.py
---------------
The one command anything points at (Render's start command, a Windows
service, a terminal). It never changes between releases: it reads
state/active-release.json, finds that release's backend and its venv,
and runs it.

    python bin/launcher.py http

Fix vs the STRUCT template: the template looked only for venv/bin/python,
so on Windows (where deploy.ps1 creates venv\\Scripts\\python.exe) it
silently fell back to whatever Python started the launcher. Both layouts
are checked now, and the choice is printed.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _active_release() -> str:
    pointer = PROJECT_ROOT / "state" / "active-release.json"
    if not pointer.exists():
        sys.exit(f"FATAL: {pointer} does not exist. Nothing to launch.")
    try:
        data = json.loads(pointer.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        sys.exit(f"FATAL: {pointer} is not valid JSON: {exc}")
    release = data.get("active_release")
    if not release:
        sys.exit(f"FATAL: {pointer} has no 'active_release' set.")
    return release


def _python_for(release_dir: Path) -> str:
    for candidate in (release_dir / "venv" / "bin" / "python", release_dir / "venv" / "Scripts" / "python.exe"):
        if candidate.exists():
            return str(candidate)
    return sys.executable  # e.g. on Render, where dependencies are installed globally at build time


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] != "http":
        sys.exit("Usage: python bin/launcher.py http")
    release = _active_release()
    release_dir = PROJECT_ROOT / "releases" / release
    backend = release_dir / "app" / "backend"
    if not backend.is_dir():
        sys.exit(f"FATAL: active_release '{release}' points at {release_dir}, which has no app/backend.")
    python_bin = _python_for(release_dir)
    print(f"[launcher] active_release={release} python={python_bin}", file=sys.stderr, flush=True)
    os.chdir(backend)
    args = [python_bin, str(backend / "entrypoints" / "http_entrypoint.py")]
    if os.name == "nt":
        import subprocess
        sys.exit(subprocess.call(args))
    os.execv(python_bin, args)


if __name__ == "__main__":
    main()
