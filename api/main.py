"""FastAPI backend for the recovery console."""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .session import RecoverySession

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

app = FastAPI(title="Recovery Console", version="0.1.0")

session = RecoverySession(
    n=int(os.environ.get("SESSION_N", 50_000)),
    days=int(os.environ.get("SESSION_DAYS", 14)),
    seed=int(os.environ.get("SESSION_SEED", 7)),
)

# Warm start so the console has content on first paint rather than an empty grid.
session.advance(1200)


class KillSwitch(BaseModel):
    engaged: bool
    scope: str = "all"


class DryRun(BaseModel):
    enabled: bool


class Approval(BaseModel):
    idempotency_key: str
    approve: bool


@app.get("/api/age-curve")
def age_curve():
    """Recovery probability bucketed by how stale the failure was."""
    return {"bands": session.age_curve()}


@app.get("/api/issuer-detail")
def issuer_detail(issuer: str, method: str):
    detail = session.issuer_detail(issuer, method)
    if not detail.get("found"):
        raise HTTPException(404, "no observed traffic for that issuer and method")
    return detail


@app.get("/api/action-required")
def action_required():
    return session.action_required()


@app.post("/api/dry-run")
def dry_run(body: DryRun):
    return session.set_dry_run(body.enabled)


@app.get("/api/stream")
def stream(step: int = Query(14, ge=0, le=200), limit: int = Query(60, ge=1, le=200)):
    """Advance the replay and return the current feed plus headline metrics."""
    if step:
        session.advance(step)
    return {
        "feed": session.feed_rows(limit),
        "metrics": session.snapshot(),
        "issuers": session.issuer_board(),
    }


@app.get("/api/metrics")
def metrics():
    return session.snapshot()


@app.get("/api/issuers")
def issuers():
    return session.issuer_board()


@app.get("/api/approvals")
def approvals(limit: int = Query(40, ge=1, le=200)):
    return {"queue": session.approvals(limit), "stats": session.guards.stats()}


@app.post("/api/approvals/resolve")
def resolve(body: Approval):
    result = session.resolve_approval(body.idempotency_key, body.approve)
    if result is None:
        raise HTTPException(404, "no queued action with that idempotency key")
    return result


@app.get("/api/audit")
def audit(limit: int = Query(120, ge=1, le=500), q: str = ""):
    return {"entries": session.audit(limit, q)}


@app.post("/api/kill-switch")
def kill_switch(body: KillSwitch):
    return session.set_kill_switch(body.engaged, body.scope)


@app.post("/api/reset")
def reset():
    session.reset()
    session.advance(1200)
    return session.snapshot()


@app.get("/healthz")
def healthz():
    return JSONResponse({"ok": True, "processed": session.totals["processed"]})


@app.get("/")
def index():
    return FileResponse(WEB_DIR / "index.html")


if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")