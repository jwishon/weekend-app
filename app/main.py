"""Weekend — weekly auto-curated guide for the Wishon household.

Phase 1 serves a static page from `data/sample-week.json`. Phase 3 will write
fresh `data/week-YYYY-MM-DD.json` files from the Wednesday cron, and this
loader will pick up the newest one automatically.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
STATIC_DIR = BASE_DIR / "static"
TEMPLATES_DIR = BASE_DIR / "app" / "templates"

# Display order for category filter pills
CATEGORY_ORDER = [
    "outdoors",
    "markets-and-festivals",
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


def load_current_week() -> dict:
    """Return the most recent week JSON. Falls back to sample-week.json."""
    weekly_files = sorted(DATA_DIR.glob("week-*.json"), reverse=True)
    path = weekly_files[0] if weekly_files else DATA_DIR / "sample-week.json"
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


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
            # Featured venues render as always-on cards above the grid.
            # Only show on "All" view so they don't get hidden by category filtering.
            "featured_venues": data.get("featured_venues", []) if not category or category == "all" else [],
        },
    )


@app.get("/healthz")
async def health() -> JSONResponse:
    return JSONResponse({"ok": True, "ts": datetime.utcnow().isoformat() + "Z"})
