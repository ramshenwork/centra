"""
entrypoints/http_entrypoint.py
------------------------------
Started by bin/launcher.py (`python bin/launcher.py http`), with the
working directory set to this release's app/backend folder.

Render sets PORT; locally it defaults to 8000.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from wiring.app_runtime import create_app  # noqa: E402

app = create_app()


def main() -> None:
    import uvicorn

    uvicorn.run(app, host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "8000")),
                proxy_headers=True, forwarded_allow_ips="*", log_level="warning")


if __name__ == "__main__":
    main()
