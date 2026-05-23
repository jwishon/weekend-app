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


def _load_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _build_prompt(today_iso: str, weekend_dates: list[str], weather_summary: dict, recent_thumbs: dict) -> str:
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
    stats: dict = {"published": False, "generated": 0, "passed": 0, "quarantined": 0, "quarantine_list": []}
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

    try:
        prompt = _build_prompt(today_iso, weekend_dates, weather_summary, recent_thumbs)
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

    if len(passed) >= MIN_ITEMS_TO_PUBLISH:
        # Generate per-item images via kie.ai for everything that passed verification.
        # Failures are non-fatal — items just go out without image_url and the
        # template falls back to a gradient placeholder.
        try:
            images.generate_for_items(passed)
        except Exception as e:
            log.exception("image generation step crashed")
            errors.append(f"image generation: {e}")
        week_data["items"] = passed
        out_path = DATA_DIR / f"week-{weekend_dates[0]}.json"
        out_path.write_text(json.dumps(week_data, indent=2, ensure_ascii=False), encoding="utf-8")
        # Quarantine record for debugging
        quar_path = DATA_DIR / f"quarantine-{weekend_dates[0]}.json"
        quar_path.write_text(json.dumps({"items": quarantined}, indent=2, ensure_ascii=False), encoding="utf-8")
        stats["published"] = True
        _set_status("ok", f"{stats['passed']} items published, {stats['quarantined']} quarantined")
        log.info("Published %s items to %s", stats["passed"], out_path)
    else:
        msg = f"only {len(passed)} items passed verification (need {MIN_ITEMS_TO_PUBLISH}); leaving prior week live"
        errors.append(msg)
        _set_status("rejected", msg)
        log.warning(msg)

    _write_run_log(weekend_dates, stats, usage, errors)
    return {"ok": stats["published"], "stats": {k: v for k, v in stats.items() if k != "quarantine_list"}, "errors": errors}
