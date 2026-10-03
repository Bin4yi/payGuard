"""Demo backend entrypoint: ``uvicorn services.api.main:app``.

Wires the app to the local (non-cloud) adapters only — see
``services/api/state.py`` for exactly what that means. It backs both the
local demo and the single-instance "Stage 0" demo deployment described in
``docs/DEPLOYMENT.md``. State lives in this process's memory, so it must run
as exactly one instance; it is not the production entrypoint.

Environment:
  PAYGUARD_SCREENER       none | laya-hf (default) | laya-local
  PAYGUARD_CORS_ORIGINS   comma-separated allowed browser origins; unset =
                          local dev servers only. Not needed when the web
                          app is served from this same origin.
  PAYGUARD_SESSION_SECRET >= 32 chars; signs demo session tokens. Unset = a
                          random per-process secret (fine: all demo state is
                          per-process anyway).
  PAYGUARD_WEB_DIST       path to the built web app (web/dist); when set it
                          is served at / so browser and API share an origin.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from services.api.app import create_app
from services.api.state import build_local_demo_state, build_screener

_LOCAL_DEV_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:4173",
    "http://127.0.0.1:4173",
]

_secret = os.environ.get("PAYGUARD_SESSION_SECRET")
_state = build_local_demo_state(session_secret=_secret.encode("utf-8") if _secret else None)
# Laya pre-screening via the free Hugging Face demo Space by default; set
# PAYGUARD_SCREENER=none to turn it off, or laya-local to run Laya on this
# machine instead (nothing leaves it).
_state.screener = build_screener(os.environ.get("PAYGUARD_SCREENER", "laya-hf"))
if hasattr(_state.screener, "warm_up"):
    _state.screener.warm_up()
app = create_app(_state)


@app.get("/healthz", include_in_schema=False)
def healthz() -> dict:
    return {"status": "ok"}


_origins_env = os.environ.get("PAYGUARD_CORS_ORIGINS")
_origins = [o.strip() for o in _origins_env.split(",") if o.strip()] if _origins_env else _LOCAL_DEV_ORIGINS
if "*" in _origins:
    raise RuntimeError("PAYGUARD_CORS_ORIGINS must list explicit origins, not '*'")
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-PayGuard-Grant"],
)

_web_dist = os.environ.get("PAYGUARD_WEB_DIST")
if _web_dist:
    _dist = Path(_web_dist).resolve()
    if not (_dist / "index.html").is_file():
        raise RuntimeError(f"PAYGUARD_WEB_DIST={_web_dist!r} has no index.html — run `npm run build` in web/")
    app.mount("/assets", StaticFiles(directory=_dist / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str) -> FileResponse:
        # API routes are registered first and win; everything else is the
        # single-page app (a real file from dist if one exists, else index).
        if path.startswith("v1/"):
            raise HTTPException(status_code=404)
        candidate = (_dist / path).resolve()
        if path and candidate.is_file() and candidate.is_relative_to(_dist):
            return FileResponse(candidate)
        return FileResponse(_dist / "index.html")
