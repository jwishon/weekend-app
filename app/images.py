"""Image sourcing for cron-produced items.

Two-tier strategy per item:
  1. Real photo — scrape og:image / twitter:image / first <img> from source_url.
     If found and looks legit (>= 300px wide, content-type is image/*), use it.
  2. AI fallback — call kie.ai with item.image_hint, save to /app/var/data/img/.

Either way the image is downloaded and saved locally so future page loads don't
depend on the venue's CDN being up.

Failures are non-fatal — items just go out without image_url and the template
falls back to the category gradient placeholder.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urljoin

import httpx

log = logging.getLogger("weekend.images")

KIE_BASE = "https://api.kie.ai"
KIE_MODEL = "google/nano-banana"
IMG_SIZE = "16:9"
IMG_DIR = Path("/app/var/data/img")
POLL_TIMEOUT_SEC = 60
POLL_INTERVAL = 2

IMG_URL_PREFIX = "/img"
BROWSER_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
MIN_IMAGE_BYTES = 8000  # smaller than this is probably a placeholder/icon


# ---------- Real-photo path (og:image scraping) ----------

OG_PATTERNS = [
    re.compile(r'<meta\s+[^>]*property=["\']og:image["\'][^>]*content=["\']([^"\']+)["\']', re.I),
    re.compile(r'<meta\s+[^>]*content=["\']([^"\']+)["\'][^>]*property=["\']og:image["\']', re.I),
    re.compile(r'<meta\s+[^>]*name=["\']twitter:image["\'][^>]*content=["\']([^"\']+)["\']', re.I),
    re.compile(r'<meta\s+[^>]*content=["\']([^"\']+)["\'][^>]*name=["\']twitter:image["\']', re.I),
]


def scrape_image_url(client: httpx.Client, source_url: str) -> str | None:
    """Pull the best representative image URL from a source page's meta tags."""
    try:
        r = client.get(source_url, headers={"User-Agent": BROWSER_UA}, timeout=15, follow_redirects=True)
        if r.status_code != 200:
            return None
        html = r.text
    except Exception as e:
        log.debug("scrape failed for %s: %s", source_url, e)
        return None

    for pat in OG_PATTERNS:
        m = pat.search(html)
        if m:
            url = m.group(1).strip()
            if url.startswith("//"):
                url = "https:" + url
            elif url.startswith("/"):
                url = urljoin(source_url, url)
            return url
    return None


def _download_image(client: httpx.Client, url: str, dest: Path) -> bool:
    """Download, verify it's a real image >= MIN_IMAGE_BYTES. Returns True on success."""
    try:
        r = client.get(url, headers={"User-Agent": BROWSER_UA}, timeout=20, follow_redirects=True)
        if r.status_code != 200:
            return False
        ct = r.headers.get("content-type", "")
        if not ct.startswith("image/"):
            return False
        if len(r.content) < MIN_IMAGE_BYTES:
            return False
        dest.write_bytes(r.content)
        return True
    except Exception as e:
        log.debug("download failed for %s: %s", url, e)
        return False


# ---------- AI fallback path (kie.ai) ----------

def _kie_key() -> str:
    k = os.environ.get("KIE_API_KEY", "")
    if not k:
        raise RuntimeError("KIE_API_KEY env var is not set")
    return k


def _kie_create_task(client: httpx.Client, prompt: str) -> str:
    payload = {
        "model": KIE_MODEL,
        "input": {"prompt": prompt, "image_size": IMG_SIZE, "output_format": "png"},
    }
    r = client.post(
        f"{KIE_BASE}/api/v1/jobs/createTask",
        headers={"Authorization": f"Bearer {_kie_key()}"},
        json=payload,
        timeout=15,
    )
    r.raise_for_status()
    body = r.json()
    return body.get("data", {}).get("taskId") or body.get("taskId")


def _kie_poll(client: httpx.Client, task_id: str) -> str:
    deadline = time.time() + POLL_TIMEOUT_SEC
    while time.time() < deadline:
        r = client.get(
            f"{KIE_BASE}/api/v1/jobs/recordInfo",
            headers={"Authorization": f"Bearer {_kie_key()}"},
            params={"taskId": task_id},
            timeout=10,
        )
        r.raise_for_status()
        data = (r.json() or {}).get("data") or {}
        state = (data.get("state") or "").lower()
        if state == "success":
            result = data.get("resultJson", "{}")
            if isinstance(result, str):
                result = json.loads(result)
            urls = result.get("resultUrls", []) or result.get("urls", [])
            if urls:
                return urls[0]
            raise RuntimeError(f"success with no urls: {data}")
        if state in ("fail", "failed", "error"):
            raise RuntimeError(f"kie failed: {data.get('failMsg') or data}")
        time.sleep(POLL_INTERVAL)
    raise TimeoutError(f"kie polling exceeded {POLL_TIMEOUT_SEC}s for {task_id}")


def kie_generate(client: httpx.Client, prompt: str, dest: Path) -> bool:
    try:
        tid = _kie_create_task(client, prompt)
        url = _kie_poll(client, tid)
        r = client.get(url, timeout=20)
        r.raise_for_status()
        dest.write_bytes(r.content)
        return True
    except Exception as e:
        log.warning("kie generation failed: %s", e)
        return False


# ---------- Top-level per-item orchestration ----------

def source_image(item: dict) -> tuple[str | None, str]:
    """Acquire an image for one item. Returns (image_url_path, source_kind).
    source_kind is 'scraped', 'ai', 'cached', or 'failed' (for logs)."""
    IMG_DIR.mkdir(parents=True, exist_ok=True)
    dest = IMG_DIR / f"{item['id']}.png"

    if dest.exists() and dest.stat().st_size >= MIN_IMAGE_BYTES:
        return f"{IMG_URL_PREFIX}/{item['id']}.png", "cached"

    with httpx.Client() as client:
        # 1. Try scraping the source page
        src = item.get("source_url")
        if src and src.startswith("http"):
            scraped = scrape_image_url(client, src)
            if scraped and _download_image(client, scraped, dest):
                log.info("[%s] used scraped og:image from %s", item["id"], src)
                return f"{IMG_URL_PREFIX}/{item['id']}.png", "scraped"

        # 2. Fall back to AI
        hint = item.get("image_hint", "")
        if hint and os.environ.get("KIE_API_KEY"):
            if kie_generate(client, hint, dest):
                log.info("[%s] used AI fallback for hint: %s", item["id"], hint[:60])
                return f"{IMG_URL_PREFIX}/{item['id']}.png", "ai"

    return None, "failed"


def generate_for_items(items: list[dict], max_workers: int = 8) -> dict:
    """Mutate items in place — set item['image_url'] for any that succeed.
    Returns a summary dict for the run log."""
    if not items:
        return {"scraped": 0, "ai": 0, "cached": 0, "failed": 0}

    todo = [it for it in items if not it.get("image_url")]
    summary = {"scraped": 0, "ai": 0, "cached": 0, "failed": 0}
    log.info("Sourcing images for %d items (pool=%d)", len(todo), max_workers)

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(source_image, it): it for it in todo}
        for f in as_completed(futures):
            item = futures[f]
            url, kind = f.result()
            summary[kind] += 1
            if url:
                item["image_url"] = url
    log.info("Image sourcing complete: %s", summary)
    return summary
