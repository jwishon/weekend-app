"""Weekend — weekly auto-curated guide for the Wishon household.

Phase 1 serves a static page from `data/sample-week.json`. Phase 2 adds
votes (preference signal) and stars (family-coordination signal) backed
by SQLite at /app/var/weekend.db. Phase 3 will write fresh
`data/week-YYYY-MM-DD.json` files from the Wednesday cron.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import os

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from app import cron as cron_mod
from app import db

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
STATIC_DIR = BASE_DIR / "static"
TEMPLATES_DIR = BASE_DIR / "app" / "templates"

# Display order for category filter pills
CATEGORY_ORDER = [
    "outdoors",
    "mcmenamins",
    "markets-and-festivals",
    "indoor-historical",
    "concerts",
    "coast",
    "gorge-and-hood",
    "south-valley",
    "family",
    "hidden-gems",
]

app = FastAPI(title="Weekend", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=TEMPLATES_DIR)


_scheduler: BackgroundScheduler | None = None


@app.on_event("startup")
def _startup() -> None:
    global _scheduler
    db.init_db()
    # Lazy cleanup: drop any stars from prior weekends so the table doesn't grow forever.
    data = load_current_week()
    weekend_start = (data.get("weekend_dates") or [""])[0]
    if weekend_start:
        db.purge_old_stars(weekend_start)
    # Scheduler — only fire if we have the API key (avoids accidental runs in dev)
    if os.environ.get("ANTHROPIC_API_KEY"):
        _scheduler = BackgroundScheduler(timezone="America/Los_Angeles")
        # Wednesday 7am Pacific
        _scheduler.add_job(cron_mod.run, CronTrigger(day_of_week="wed", hour=7, minute=0),
                           id="weekly-research", coalesce=True, max_instances=1)
        _scheduler.start()


@app.on_event("shutdown")
def _shutdown() -> None:
    if _scheduler:
        _scheduler.shutdown(wait=False)


VAR_DATA_DIR = Path("/app/var/data")  # cron-published weeks live here (persists on QNAP)


def load_current_week() -> dict:
    """Return the most recent week JSON. Prefers cron-published weeks in the
    persistent volume; falls back to the in-image sample-week.json."""
    if VAR_DATA_DIR.exists():
        weekly_files = sorted(VAR_DATA_DIR.glob("week-*.json"), reverse=True)
        if weekly_files:
            with weekly_files[0].open("r", encoding="utf-8") as f:
                return json.load(f)
    with (DATA_DIR / "sample-week.json").open("r", encoding="utf-8") as f:
        return json.load(f)


def _weekend_start() -> str:
    """The first date of the current weekend — the partition key for votes/stars."""
    dates = load_current_week().get("weekend_dates") or []
    if not dates:
        # Defensive fallback; shouldn't hit in normal operation
        return datetime.utcnow().date().isoformat()
    return dates[0]


# ----- page -----

@app.get("/", response_class=HTMLResponse)
async def home(request: Request, category: str | None = None) -> HTMLResponse:
    data = load_current_week()
    all_items = data.get("items", [])

    if category and category != "all":
        items = [i for i in all_items if i.get("category") == category]
    else:
        items = all_items

    present = {i["category"] for i in all_items}
    categories = [c for c in CATEGORY_ORDER if c in present]

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "data": data,
            "items": items,
            "categories": categories,
            "active_category": category or "all",
            "weekend_dates": data.get("weekend_dates", []),
            "weather_summary": data.get("weather_summary", {}),
            "featured_venues": data.get("featured_venues", []) if not category or category == "all" else [],
            "voters": sorted(db.VALID_VOTERS),
        },
    )


# ----- API -----

class VoteIn(BaseModel):
    item_id: str = Field(min_length=1)
    direction: str  # "up" | "down" | "clear"
    voter_tag: str


class StarIn(BaseModel):
    item_id: str = Field(min_length=1)
    voter_tag: str


@app.post("/vote")
def post_vote(payload: VoteIn) -> JSONResponse:
    try:
        if payload.direction == "clear":
            db.clear_vote(payload.item_id, payload.voter_tag, _weekend_start())
        else:
            db.record_vote(payload.item_id, payload.direction, payload.voter_tag, _weekend_start())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return JSONResponse(_state_for_item(payload.item_id))


@app.post("/star")
def post_star(payload: StarIn) -> JSONResponse:
    try:
        db.set_star(payload.item_id, payload.voter_tag, _weekend_start())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return JSONResponse(_state_for_item(payload.item_id))


@app.delete("/star")
def delete_star(payload: StarIn) -> JSONResponse:
    try:
        db.clear_star(payload.item_id, payload.voter_tag, _weekend_start())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return JSONResponse(_state_for_item(payload.item_id))


@app.get("/api/state")
def get_api_state() -> JSONResponse:
    return JSONResponse({
        "weekend_start": _weekend_start(),
        "items": db.get_state(_weekend_start()),
    })


@app.get("/preferences")
def get_preferences(weeks: int = 8) -> JSONResponse:
    return JSONResponse({
        "window_weeks": weeks,
        "items": db.get_preferences(weeks),
    })


def _state_for_item(item_id: str) -> dict:
    """Return a single item's current state — used as the response for write endpoints
    so the client can update without a separate GET."""
    state = db.get_state(_weekend_start())
    return {"item_id": item_id, "state": state.get(item_id, {"up": 0, "down": 0, "stars": [], "votes_by": {}})}


# ----- admin -----

@app.post("/admin/run-cron")
def admin_run_cron(x_cron_secret: str = Header(default="")) -> JSONResponse:
    """Manual trigger for the Wednesday research pipeline. Requires X-Cron-Secret header
    matching the CRON_SECRET env var. Use for testing and recovery after failed runs.
    Runs synchronously — returns when the pipeline finishes (typically 30s-2min)."""
    expected = os.environ.get("CRON_SECRET", "")
    if not expected:
        raise HTTPException(status_code=503, detail="CRON_SECRET not configured")
    if x_cron_secret != expected:
        raise HTTPException(status_code=401, detail="bad secret")
    result = cron_mod.run()
    return JSONResponse(result)


# ----- health -----

@app.get("/healthz")
async def health() -> JSONResponse:
    return JSONResponse({"ok": True, "ts": datetime.utcnow().isoformat() + "Z"})
