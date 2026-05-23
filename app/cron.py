"""Wednesday research cron — orchestrates Claude API call, verification, and publish.

Pipeline (top to bottom):
  1. Compute upcoming weekend dates (Fri/Sat/Sun) and 'today' (Wednesday).
  2. Fetch NWS weather forecasts for the weekend dates.
  3. Pull recent thumbs aggregate from SQLite for the 'taste signal'.
  4. Read prompt template + Atlas-baked context files.
  5. Substitute all {{VARS}} into the prompt.
  6. Call Claude API with web search enabled.
  7. Parse the JSON response.
  8. Run verifier — split into passed + quarantined.
  9. Gate: if passed >= MIN_ITEMS_TO_PUBLISH, write data/week-YYYY-MM-DD.json
     and the page goes live with new content. Otherwise leave prior week in place.
  10. Always write a run log to /app/var/logs/YYYY-MM-DD-run.md.
  11. Set a 'last_run_status' marker in SQLite so the page (and future ops) can
      surface "last cron failed, content is stale" if needed.

The cron is idempotent and re-runnable. Manual trigger via POST /admin/run-cron
with the CRON_SECRET header is intended for testing and recovery.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from anthropic import Anthropic

from app import db, images, verify, weather

log = logging.getLogger("weekend.cron")

REPO_ROOT = Path(__file__).resolve().parent.parent
PROMPT_PATH = REPO_ROOT / "prompts" / "wednesday-research.md"
INPUTS_DIR = REPO_ROOT / "prompts" / "inputs"
# In-image directory has the sample-week.json fallback. Cron output goes to
# /app/var/data/ which is bind-mounted to QNAP and survives container restarts.
DATA_DIR = Path("/app/var/data")
LOG_DIR = Path("/app/var/logs")

MIN_ITEMS_TO_PUBLISH = 6  # below this, keep prior week live
CLAUDE_MODEL = "claude-sonnet-4-5"
MAX_TOKENS = 16000

# Pacific timezone — Wednesday 7am Pacific is the canonical fire time
PACIFIC = ZoneInfo("America/Los_Angeles")


def upcoming_weekend(today: date | None = None) -> list[str]:
    """Return [Fri, Sat, Sun] of the weekend we should be planning for.

    Rule:
    - Monday/Tuesday/Wednesday → the immediately upcoming weekend (this Fri/Sat/Sun)
    - Thursday/Friday/Saturday/Sunday → the current weekend window (the Fri/Sat/Sun
      that contains today, or the most recent past Friday for Thu)

    This way the scheduled Wednesday 7am cron correctly fetches the weekend 2 days
    out, AND a manual mid-weekend test fetches the weekend we're actually in.
    """
    today = today or datetime.now(PACIFIC).date()
    # Friday offset: positive when Friday is upcoming, zero on Friday, negative when
    # we're mid-weekend or just past it. Mon=4d, Tue=3d, Wed=2d (canonical cron),
    # Thu=1d, Fri=0d, Sat=-1d, Sun=-2d.
    days_to_fri = 4 - today.weekday()
    fri = today + timedelta(days=days_to_fri)
    return [(fri + timedelta(days=i)).isoformat() for i in range(3)]


def _calendar_context(weekend_dates: list[str]) -> str:
    """One-paragraph description of any holiday or cultural theme for the weekend.

    The window scanned is Fri–Mon so observed-on-Monday holidays (Memorial Day,
    Labor Day) trigger correctly even though weekend_dates is Fri–Sun. Returns
    "Regular weekend." when nothing special matches.
    """
    if not weekend_dates:
        return "Regular weekend."
    try:
        fri = datetime.strptime(weekend_dates[0], "%Y-%m-%d").date()
    except ValueError:
        return "Regular weekend."
    window = [fri + timedelta(days=i) for i in range(4)]  # Fri..Mon

    for d in window:
        # Memorial Day — last Monday of May
        if d.month == 5 and d.weekday() == 0 and d.day >= 25:
            return (f"Memorial Day weekend (May {d.day} observed). 3-day federal holiday. "
                    "Themes: veterans, military tributes, flag ceremonies (Willamette National Cemetery in Happy Valley), "
                    "start-of-summer outdoor push, Willamette Valley Memorial Day winery open house weekend, "
                    "parades, patriotic music, cemetery events.")
        # Independence Day
        if d.month == 7 and d.day == 4:
            return ("Independence Day weekend. Themes: fireworks shows (Waterfront Blues Festival, "
                    "Oaks Park, Fort Vancouver), parades, patriotic events, outdoor festivals, BBQ, "
                    "summer fairs.")
        # Labor Day — first Monday of September
        if d.month == 9 and d.weekday() == 0 and d.day <= 7:
            return ("Labor Day weekend. 3-day federal holiday. Themes: end-of-summer outdoor push, "
                    "fall festivals starting, last big beach weekends, last winery weekend before harvest, "
                    "Pendleton Round-Up vibes (statewide).")
        # Veterans Day
        if d.month == 11 and d.day == 11:
            return ("Veterans Day. Themes: military tributes, ceremonies at Willamette National Cemetery, "
                    "free admission for veterans at many museums and attractions, parades.")
        # Thanksgiving — 4th Thursday of November
        if d.month == 11 and d.weekday() == 3 and 22 <= d.day <= 28:
            return ("Thanksgiving weekend. 4-day federal holiday. Themes: holiday markets, "
                    "tree-lighting kickoffs (Pioneer Courthouse Square, Pittock Mansion), Zoo Lights, "
                    "Black Friday events, indoor family activities for cold weather.")
        # Christmas weekend
        if d.month == 12 and 24 <= d.day <= 26:
            return ("Christmas weekend. Themes: holiday lights (Zoo Lights, Peacock Lane, Winter Wonderland PIR), "
                    "Christmas markets, Pittock Mansion holiday tours, Holiday Express train at OERHS, "
                    "tree lighting events, family indoor activities.")
        # New Year's
        if d.month == 12 and d.day == 31:
            return ("New Year's Eve weekend. Themes: NYE parties, midnight fireworks at the waterfront, "
                    "First Run resolutions runs, family-friendly noon countdowns.")
    return "Regular weekend."


def _load_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _build_prompt(today_iso: str, weekend_dates: list[str], weather_summary: dict, recent_thumbs: dict, calendar_context: str) -> str:
    template = _load_text(PROMPT_PATH)
    # Strip the human header sections — only send the actual prompt body
    # (everything from '---' onward in the file)
    if "\n---\n" in template:
        template = template.split("\n---\n", 1)[1].strip()

    seed_likes = _load_text(INPUTS_DIR / "seed-likes.md")
    pinned_sources = _load_text(INPUTS_DIR / "pinned-sources.md")
    family_profile = _load_text(INPUTS_DIR / "family-profile.md")

    return (
        template
        .replace("{{TODAY}}", today_iso)
        .replace("{{WEEKEND_DATES}}", " / ".join(weekend_dates))
        .replace("{{WEATHER_FORECAST}}", json.dumps(weather_summary, indent=2))
        .replace("{{CALENDAR_CONTEXT}}", calendar_context)
        .replace("{{SEED_LIKES}}", seed_likes)
        .replace("{{PINNED_SOURCES}}", pinned_sources)
        .replace("{{FAMILY_PROFILE}}", family_profile)
        .replace("{{RECENT_THUMBS}}", json.dumps(recent_thumbs, indent=2) if recent_thumbs else "(no thumbs data yet)")
    )


def _call_claude(prompt: str) -> tuple[str, dict]:
    """Returns (response_text, usage_info)."""
    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    resp = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=MAX_TOKENS,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=[{"role": "user", "content": prompt}],
    )
    # Concatenate text blocks (skip tool_use blocks)
    text_parts = [b.text for b in resp.content if getattr(b, "type", None) == "text"]
    return ("".join(text_parts), {
        "input_tokens": resp.usage.input_tokens,
        "output_tokens": resp.usage.output_tokens,
        "stop_reason": resp.stop_reason,
    })


def _extract_json(text: str) -> dict:
    """Pull the first JSON object from Claude's response.
    Claude may wrap JSON in ```json fences or include leading prose despite the
    'JSON only' instruction. Strip both."""
    t = text.strip()
    # Strip markdown code fences if present
    if t.startswith("```"):
        first_nl = t.find("\n")
        t = t[first_nl + 1:]
        if t.endswith("```"):
            t = t[:-3]
        t = t.strip()
    # Find first '{' and try to parse from there
    start = t.find("{")
    if start == -1:
        raise ValueError("No JSON object found in response")
    # Greedy: take to last '}' first, fall back if needed
    end = t.rfind("}")
    candidate = t[start:end + 1]
    return json.loads(candidate)


def _normalize_week_data(week_data: dict) -> dict:
    """Accept common Claude field-name drift (curated_items vs items, location vs where,
    meta.special_context vs calendar_context) and remap to the canonical schema. Without
    this, a perfectly good 22-item generation gets dropped on the floor because Claude
    decided to be creative with key names."""

    # Top-level: pull calendar_context out of meta if Claude wrapped it
    if "calendar_context" not in week_data:
        meta = week_data.get("meta") or {}
        if isinstance(meta, dict):
            week_data["calendar_context"] = (
                meta.get("special_context") or meta.get("calendar_context") or ""
            )

    # items: accept curated_items as alias
    if "items" not in week_data and "curated_items" in week_data:
        week_data["items"] = week_data["curated_items"]

    # featured_venues + venue_scan_log: split a single venue_scan array by status
    if "featured_venues" not in week_data and "venue_scan" in week_data:
        featured, scan_log = [], []
        for v in week_data.get("venue_scan", []) or []:
            status = v.get("status") or v.get("state") or "no_event"
            event = v.get("event") or None
            if status == "has_event" and event:
                featured.append({
                    "id": (v.get("venue") or "venue").lower().replace(" ", "-"),
                    "name": v.get("venue") or "",
                    "where": v.get("address") or v.get("location") or v.get("where") or "",
                    "source_url": v.get("url") or v.get("source_url") or "",
                    "event": {
                        "title": event.get("name") or event.get("title") or "",
                        "when": event.get("dates") or event.get("when") or "",
                        "why": event.get("description") or event.get("why") or "",
                        "audience_tags": event.get("audience_tags") or (
                            [event["audience_fit"]] if event.get("audience_fit") else []
                        ),
                        "image_hint": event.get("image_hint") or "",
                    },
                })
            else:
                scan_log.append({
                    "venue": v.get("venue") or "",
                    "url": v.get("url") or v.get("source_url") or "",
                    "state": status,
                    "note": v.get("notes") or v.get("note") or "",
                })
        week_data["featured_venues"] = featured
        week_data["venue_scan_log"] = scan_log

    # Per-item field name normalization
    for item in week_data.get("items", []) or []:
        if "where" not in item and "location" in item:
            item["where"] = item["location"]
        if "why" not in item:
            item["why"] = item.get("description") or item.get("why_it_fits") or ""
        if "audience_tags" not in item or not isinstance(item.get("audience_tags"), list):
            item["audience_tags"] = []
        # Claude sometimes uses non-canonical categories. Map known variants.
        cat = item.get("category", "")
        if cat in ("holiday", "teen", "date", "kid-friendly", "adults-only"):
            tag = "date-night" if cat == "date" else cat
            if tag not in item["audience_tags"]:
                item["audience_tags"].append(tag)
            # Holiday items default to family unless something more specific is implied
            item["category"] = "family"
        elif cat == "wine-and-beer":
            item["category"] = "south-valley"
        elif cat in ("indoors-and-rainy-day", "indoor"):
            item["category"] = "indoor-historical"

    return week_data


def _write_run_log(weekend_dates: list[str], stats: dict, usage: dict, errors: list[str]) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"{datetime.now(PACIFIC).date().isoformat()}-run.md"
    cost_estimate = (usage.get("input_tokens", 0) * 3 + usage.get("output_tokens", 0) * 15) / 1_000_000  # rough Sonnet pricing
    body = f"""# Cron run — {datetime.now(PACIFIC).isoformat()}

**Weekend:** {', '.join(weekend_dates)}

## Result
- Published: {'YES' if stats.get('published') else 'NO — kept prior week live'}
- Items generated: {stats.get('generated', 0)}
- Items passed verification: {stats.get('passed', 0)}
- Items quarantined: {stats.get('quarantined', 0)}

## Images
- Generated (item images): {stats['images'].get('generated', 0)}
- Featured venue images: {stats['images'].get('featured_venue_images', 0)}
- Failed: {stats['images'].get('failed', 0)}

## Token usage
- Input: {usage.get('input_tokens', 0)}
- Output: {usage.get('output_tokens', 0)}
- Estimated cost (Sonnet rates): ${cost_estimate:.3f}
- Stop reason: {usage.get('stop_reason', '?')}

## Quarantine
{chr(10).join(f"- **{q['id']}** — {q['_verify_reason']}" for q in stats.get('quarantine_list', [])) or '(none)'}

## Errors
{chr(10).join(f"- {e}" for e in errors) or '(none)'}
"""
    log_path.write_text(body, encoding="utf-8")
    log.info("Run log written to %s", log_path)


def _set_status(status: str, note: str = "") -> None:
    """Set the last-run status flag in SQLite for the page to surface."""
    DB_PATH = Path("/app/var/weekend.db")
    with sqlite3.connect(str(DB_PATH)) as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS cron_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL DEFAULT (datetime('now')),
                status TEXT NOT NULL,
                note TEXT
            )"""
        )
        conn.execute("INSERT INTO cron_runs (status, note) VALUES (?, ?)", (status, note))
        conn.commit()


def run() -> dict:
    """Execute one cron pipeline. Returns a summary dict."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    stats: dict = {"published": False, "generated": 0, "passed": 0, "quarantined": 0,
                   "quarantine_list": [], "images": {"generated": 0, "skipped": 0, "failed": 0, "featured_venue_images": 0}}
    usage: dict = {}

    weekend_dates = upcoming_weekend()
    today_iso = datetime.now(PACIFIC).date().isoformat()
    log.info("Cron run starting — weekend %s", weekend_dates)

    try:
        weather_summary = weather.fetch_summary(weekend_dates)
    except Exception as e:
        log.exception("Weather fetch failed; continuing with empty summary")
        weather_summary = {"hillsboro": "(unavailable)", "coast": "(unavailable)", "gorge": "(unavailable)", "mt-hood": "(unavailable)"}
        errors.append(f"weather: {e}")

    try:
        recent_thumbs = db.get_preferences(weeks=8)
    except Exception as e:
        log.exception("Preferences fetch failed; continuing with empty thumbs")
        recent_thumbs = {}
        errors.append(f"thumbs: {e}")

    calendar_context = _calendar_context(weekend_dates)
    log.info("Calendar context: %s", calendar_context[:120])

    try:
        prompt = _build_prompt(today_iso, weekend_dates, weather_summary, recent_thumbs, calendar_context)
    except Exception as e:
        errors.append(f"prompt assembly: {e}")
        _set_status("error", "; ".join(errors))
        _write_run_log(weekend_dates, stats, usage, errors)
        return {"ok": False, "stats": stats, "errors": errors}

    try:
        response_text, usage = _call_claude(prompt)
    except Exception as e:
        errors.append(f"Claude API: {e}")
        _set_status("error", "; ".join(errors))
        _write_run_log(weekend_dates, stats, usage, errors)
        return {"ok": False, "stats": stats, "errors": errors}

    try:
        week_data = _extract_json(response_text)
        week_data = _normalize_week_data(week_data)
    except Exception as e:
        errors.append(f"JSON parse: {e}")
        # Save the raw response for debugging
        (LOG_DIR / f"{today_iso}-raw.txt").write_text(response_text, encoding="utf-8")
        _set_status("error", "; ".join(errors))
        _write_run_log(weekend_dates, stats, usage, errors)
        return {"ok": False, "stats": stats, "errors": errors}

    # Make sure dates match what we asked for (defensive — Claude sometimes drifts)
    week_data["weekend_dates"] = weekend_dates
    if "weather_summary" not in week_data or not week_data["weather_summary"]:
        week_data["weather_summary"] = weather_summary

    stats["generated"] = len(week_data.get("items", []))

    try:
        passed, quarantined = verify.verify_week(week_data)
    except Exception as e:
        errors.append(f"verifier crashed: {e}")
        # Treat as full failure — don't publish unverified content
        _set_status("error", "; ".join(errors))
        _write_run_log(weekend_dates, stats, usage, errors)
        return {"ok": False, "stats": stats, "errors": errors}

    stats["passed"] = len(passed)
    stats["quarantined"] = len(quarantined)
    stats["quarantine_list"] = quarantined

    # ALWAYS persist diagnostics — draft (raw week_data) + quarantine list — even on rejection.
    # Without this, debugging a 0-items-passed run is impossible.
    draft_path = DATA_DIR / f"draft-{weekend_dates[0]}.json"
    draft_path.write_text(json.dumps(week_data, indent=2, ensure_ascii=False), encoding="utf-8")
    quar_path = DATA_DIR / f"quarantine-{weekend_dates[0]}.json"
    quar_path.write_text(json.dumps({"items": quarantined}, indent=2, ensure_ascii=False), encoding="utf-8")

    if len(passed) >= MIN_ITEMS_TO_PUBLISH:
        # Generate AI images for everything that passed verification + any
        # featured venue events that came back populated. Non-fatal: items
        # without an image just render with the category gradient placeholder,
        # featured venues without an image fall back to the empty-state asset.
        try:
            img_summary = images.generate_for_items(passed)
            venue_count = images.generate_for_featured_venues(week_data.get("featured_venues", []))
            stats["images"] = {**img_summary, "featured_venue_images": venue_count}
        except Exception as e:
            log.exception("image generation step crashed")
            errors.append(f"image generation: {e}")
        week_data["items"] = passed
        out_path = DATA_DIR / f"week-{weekend_dates[0]}.json"
        out_path.write_text(json.dumps(week_data, indent=2, ensure_ascii=False), encoding="utf-8")
        stats["published"] = True
        _set_status("ok", f"{stats['passed']} items published, {stats['quarantined']} quarantined")
        log.info("Published %s items to %s", stats["passed"], out_path)
    else:
        msg = f"generated {stats['generated']}, only {len(passed)} passed verification (need {MIN_ITEMS_TO_PUBLISH}); leaving prior week live"
        errors.append(msg)
        _set_status("rejected", msg)
        log.warning(msg)

    _write_run_log(weekend_dates, stats, usage, errors)
    return {"ok": stats["published"], "stats": {k: v for k, v in stats.items() if k != "quarantine_list"}, "errors": errors}
