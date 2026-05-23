"""NWS forecast fetcher — no API key needed, polite User-Agent required.

We resolve each lat/lon to the NWS gridpoint forecast endpoint (cached for the
container's lifetime since these don't change) and then pull the upcoming
periods. The cron only needs a one-line human summary per region, so we
return that shape directly.

NWS API conventions: https://www.weather.gov/documentation/services-web-api
"""

from __future__ import annotations

import json
import logging
from typing import Dict

import httpx

log = logging.getLogger(__name__)

USER_AGENT = "weekend-app/0.1 (wishon@gmail.com)"
HEADERS = {"User-Agent": USER_AGENT, "Accept": "application/geo+json"}
NWS = "https://api.weather.gov"

# Regions we care about — keys match the JSON schema the page consumes.
REGIONS: Dict[str, tuple[float, float, str]] = {
    "hillsboro":      (45.5229, -122.9898, "Hillsboro"),
    "coast":          (45.7188, -123.9347, "Manzanita"),
    "gorge":          (45.7054, -121.5215, "Hood River"),
    "mt-hood":        (45.3036, -121.7560, "Government Camp"),
}

# Cache gridpoint URLs across runs (they're stable per coordinate)
_grid_cache: dict[tuple[float, float], str] = {}


def _resolve_gridpoint(client: httpx.Client, lat: float, lon: float) -> str:
    key = (round(lat, 4), round(lon, 4))
    if key in _grid_cache:
        return _grid_cache[key]
    r = client.get(f"{NWS}/points/{lat:.4f},{lon:.4f}", headers=HEADERS, timeout=10)
    r.raise_for_status()
    forecast_url = r.json()["properties"]["forecast"]
    _grid_cache[key] = forecast_url
    return forecast_url


def _summarize_periods(periods: list[dict], dates: list[str]) -> str:
    """Reduce NWS period array to a one-line summary covering the weekend dates."""
    relevant = []
    for p in periods:
        # Each period has 'startTime' like '2026-05-23T06:00:00-07:00' and 'isDaytime', 'shortForecast', 'temperature'
        start = p.get("startTime", "")[:10]
        if start in dates and p.get("isDaytime"):
            relevant.append(p)
    if not relevant:
        # Fall back to the next daytime period we have
        relevant = [p for p in periods if p.get("isDaytime")][:1]
    if not relevant:
        return "(no forecast)"
    parts = []
    for p in relevant[:3]:
        parts.append(f"{p.get('shortForecast','?')}, {p.get('temperature','?')}°{p.get('temperatureUnit','F')}")
    # Deduplicate consecutive identical phrases
    out = []
    for x in parts:
        if not out or out[-1] != x:
            out.append(x)
    return "; ".join(out)


def fetch_summary(weekend_dates: list[str]) -> Dict[str, str]:
    """Return {region_key: one-line summary} for the given weekend dates.
    On failure for any region, that key carries an error string instead.
    """
    out: Dict[str, str] = {}
    with httpx.Client() as client:
        for key, (lat, lon, label) in REGIONS.items():
            try:
                fc_url = _resolve_gridpoint(client, lat, lon)
                r = client.get(fc_url, headers=HEADERS, timeout=10)
                r.raise_for_status()
                periods = r.json()["properties"]["periods"]
                out[key] = _summarize_periods(periods, weekend_dates)
            except Exception as e:
                log.warning("Weather fetch failed for %s: %s", label, e)
                out[key] = f"(forecast unavailable)"
    return out


if __name__ == "__main__":
    # Smoke test
    import sys
    dates = sys.argv[1:] or ["2026-05-23", "2026-05-24", "2026-05-25"]
    print(json.dumps(fetch_summary(dates), indent=2))
