"""Post-generation verification gate for the Wednesday cron output.

Catches the failure modes John flagged manually:
- Item links to a venue page where the dated event isn't actually on that date
- Item names an Oregon venue but the page is for a Washington location
- Item is hallucinated entirely with a plausible-looking source_url

Calibration trade-offs:
- Skip date check for explicitly evergreen items ("Daily", "Anytime"). Their
  source URLs are venue homepages, not dated event pages.
- Washington-trap check uses count comparison instead of any-mention — chain
  venue sites (McMenamins) list all their locations in footers, including
  Kalama and Tacoma. Only fail if WA mentions outweigh OR mentions.
- Retry once with a browser User-Agent on 4xx — some venue sites block
  default Python agents.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

import httpx

log = logging.getLogger(__name__)

BOT_UA = "weekend-app-verify/0.1 (wishon@gmail.com)"
BROWSER_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"

OREGON_CITIES = {
    "hillsboro", "beaverton", "portland", "tigard", "tualatin", "forest grove",
    "troutdale", "gresham", "lake oswego", "wilsonville", "sherwood",
    "newberg", "mcminnville", "yamhill", "carlton", "dundee", "dayton",
    "salem", "silverton", "stayton", "albany", "corvallis", "eugene", "springfield",
    "tillamook", "manzanita", "cannon beach", "rockaway", "nehalem", "wheeler",
    "hood river", "cascade locks", "the dalles", "mosier",
    "government camp", "mt. hood", "mt hood", "timberline",
}

WASHINGTON_TRAPS = {
    "bothell", "tacoma", "centralia", "kalama",
    "anderson school", "spanish ballroom", "elks temple",
    "olympic club", "kalama harbor",
}

EVERGREEN_MARKERS = {"daily", "anytime", "all day", "year-round", "ongoing", "all weekend", "varies", "all month", "throughout"}


@dataclass
class VerifyResult:
    ok: bool
    reason: str = ""


def _date_aliases(date_iso: str) -> list[str]:
    d = datetime.strptime(date_iso, "%Y-%m-%d")
    forms = {
        date_iso,
        d.strftime("%B %d, %Y"),
        d.strftime("%B %d"),
        d.strftime("%b %d, %Y"),
        d.strftime("%b %d"),
        d.strftime("%A, %B %d"),
        d.strftime("%a, %b %d"),
        d.strftime("%A %B %d"),
    }
    # Also add no-zero-padded variants
    extras = set()
    for f in forms:
        no_zero = re.sub(r'\b0(\d)', r'\1', f)
        if no_zero != f:
            extras.add(no_zero)
    return list(forms | extras)


def _normalize(s: str) -> str:
    return re.sub(r'\s+', ' ', s.lower())


def _count_mentions(text: str, terms: set[str]) -> int:
    return sum(text.count(t) for t in terms)


def _extract_city(where: str) -> str:
    """Best-effort city extraction from a 'where' field like 'McMenamins Edgefield,
    2126 SW Halsey St, Troutdale'. Returns the last comma-separated token, lowercased."""
    if not where:
        return ""
    parts = [p.strip() for p in where.split(",") if p.strip()]
    if not parts:
        return ""
    # Drop trailing state codes ('OR') if present
    last = parts[-1]
    if last.upper() in ("OR", "OREGON", "OR.", "OREGON,"):
        if len(parts) >= 2:
            last = parts[-2]
        else:
            return ""
    return last.lower()


def _is_evergreen(item: dict) -> bool:
    when = (item.get("when") or "").lower()
    return any(marker in when for marker in EVERGREEN_MARKERS)


def _fetch(client: httpx.Client, url: str) -> httpx.Response | None:
    """Try BOT_UA first; if blocked, retry with BROWSER_UA."""
    for ua in (BOT_UA, BROWSER_UA):
        try:
            r = client.get(url, headers={"User-Agent": ua}, timeout=15, follow_redirects=True)
            if r.status_code == 200:
                return r
            if 400 <= r.status_code < 500 and ua == BOT_UA:
                continue  # try browser UA
            return r
        except Exception as e:
            log.warning("Fetch error for %s with UA %s: %s", url, ua[:20], e)
            if ua == BROWSER_UA:
                return None
    return None


def verify_item(item: dict, weekend_dates: Iterable[str], client: httpx.Client) -> VerifyResult:
    src = item.get("source_url", "")
    if not src or not src.startswith("http"):
        return VerifyResult(False, "missing or invalid source_url")

    r = _fetch(client, src)
    if r is None:
        return VerifyResult(False, "fetch failed (DNS / network)")
    if r.status_code != 200:
        return VerifyResult(False, f"source returned HTTP {r.status_code}")

    page = _normalize(r.text)

    # Geography: first try to match the item's stated city. If the page mentions
    # the specific city from item.where, the page is about that location regardless
    # of footer links to other venues. Falls back to general Oregon-presence check.
    where_city = _extract_city(item.get("where", ""))
    if where_city and where_city in page:
        # Specific city match — trust it. Skip the count-based check.
        pass
    else:
        or_count = _count_mentions(page, OREGON_CITIES)
        wa_count = _count_mentions(page, WASHINGTON_TRAPS)
        if or_count == 0:
            return VerifyResult(False, "no Oregon-scope city found on source page")
        if wa_count > or_count:
            return VerifyResult(False, f"WA traps outweigh OR mentions ({wa_count} vs {or_count})")

    # Date: skip for evergreen items where item.when is "Daily" etc.
    if _is_evergreen(item):
        return VerifyResult(True)

    date_hit = False
    for d in weekend_dates:
        for alias in _date_aliases(d):
            if _normalize(alias) in page:
                date_hit = True
                break
        if date_hit:
            break
    if not date_hit:
        return VerifyResult(False, "no weekend date string found on source page")

    return VerifyResult(True)


def verify_week(week_data: dict) -> tuple[list[dict], list[dict]]:
    weekend_dates = week_data.get("weekend_dates", [])
    items = week_data.get("items", [])
    passed, quarantined = [], []
    with httpx.Client() as client:
        for item in items:
            result = verify_item(item, weekend_dates, client)
            if result.ok:
                passed.append(item)
            else:
                q = dict(item)
                q["_verify_reason"] = result.reason
                quarantined.append(q)
                log.info("Quarantined %s: %s", item.get("id"), result.reason)
    return passed, quarantined


if __name__ == "__main__":
    import json, sys
    path = sys.argv[1] if len(sys.argv) > 1 else "data/sample-week.json"
    with open(path) as f:
        data = json.load(f)
    passed, quar = verify_week(data)
    print(f"\nPassed: {len(passed)}/{len(data['items'])}")
    for p in passed:
        print(f"  ✓ {p['id']}")
    if quar:
        print(f"\nQuarantined:")
        for q in quar:
            print(f"  ✗ {q['id']}: {q['_verify_reason']}")
