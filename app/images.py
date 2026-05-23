"""AI image generation for cron-produced items via kie.ai.

Earlier iteration tried og:image scraping first, AI as fallback. In practice
real photos were a coin flip — sometimes great venue marketing shots, sometimes
the venue's logo or a generic site header. AI output with kie's nano-banana
model on a specific image_hint is more consistent. Switched to AI-only.

If we ever want real photos back, the previous version of this file (in git
before Phase 3.5) had a working scrape pipeline that can be revived.

Generation per-item is fire-and-poll; we run them in parallel via a small
thread pool to keep total cron time bounded (12 items × ~10s each = ~2min
sequential, ~30s parallel with pool=8).
"""

from __future__ import annotations

import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import httpx

log = logging.getLogger("weekend.images")

KIE_BASE = "https://api.kie.ai"
KIE_MODEL = "google/nano-banana"
IMG_SIZE = "16:9"
IMG_DIR = Path("/app/var/data/img")
POLL_TIMEOUT_SEC = 60
POLL_INTERVAL = 2

IMG_URL_PREFIX = "/img"
MIN_IMAGE_BYTES = 8000


def _kie_key() -> str:
    k = os.environ.get("KIE_API_KEY", "")
    if not k:
        raise RuntimeError("KIE_API_KEY env var is not set")
    return k


def _create_task(client: httpx.Client, prompt: str) -> str:
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


def _poll(client: httpx.Client, task_id: str) -> str:
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
            raise RuntimeError(f"kie success with no urls: {data}")
        if state in ("fail", "failed", "error"):
            raise RuntimeError(f"kie failed: {data.get('failMsg') or data}")
        time.sleep(POLL_INTERVAL)
    raise TimeoutError(f"kie polling exceeded {POLL_TIMEOUT_SEC}s for {task_id}")


def _generate_one(item_id: str, prompt: str) -> str | None:
    """Generate a single image, save to /app/var/data/img/<id>.png.
    Returns the URL path or None on failure. Idempotent — skips if file exists."""
    IMG_DIR.mkdir(parents=True, exist_ok=True)
    dest = IMG_DIR / f"{item_id}.png"
    if dest.exists() and dest.stat().st_size >= MIN_IMAGE_BYTES:
        return f"{IMG_URL_PREFIX}/{item_id}.png"
    try:
        with httpx.Client() as client:
            tid = _create_task(client, prompt)
            url = _poll(client, tid)
            r = client.get(url, timeout=20)
            r.raise_for_status()
            dest.write_bytes(r.content)
        log.info("[%s] AI-generated image (%d bytes)", item_id, dest.stat().st_size)
        return f"{IMG_URL_PREFIX}/{item_id}.png"
    except Exception as e:
        log.warning("[%s] AI generation failed: %s", item_id, e)
        # Clean up any tiny/partial file
        try:
            if dest.exists() and dest.stat().st_size < MIN_IMAGE_BYTES:
                dest.unlink()
        except Exception:
            pass
        return None


def generate_for_items(items: list[dict], max_workers: int = 8) -> dict:
    """Mutate items in place: set item['image_url'] for any that succeed."""
    if not os.environ.get("KIE_API_KEY"):
        log.warning("KIE_API_KEY not set — skipping image generation")
        return {"generated": 0, "skipped": len(items), "failed": 0}

    todo = [it for it in items if it.get("image_hint") and not it.get("image_url")]
    summary = {"generated": 0, "skipped": len(items) - len(todo), "failed": 0}
    if not todo:
        return summary

    log.info("Generating %d images in parallel (pool=%d)", len(todo), max_workers)
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(_generate_one, it["id"], it["image_hint"]): it for it in todo}
        for f in as_completed(futures):
            item = futures[f]
            url = f.result()
            if url:
                item["image_url"] = url
                summary["generated"] += 1
            else:
                summary["failed"] += 1
    log.info("Image generation complete: %s", summary)
    return summary


def generate_for_featured_venues(venues: list[dict]) -> int:
    """Featured-venue events also get AI images. The 'event' block sits inside
    each venue dict; we generate from event.image_hint and set event.image_url.
    Returns count generated."""
    if not os.environ.get("KIE_API_KEY"):
        return 0
    count = 0
    for v in venues or []:
        event = v.get("event")
        if not event or event.get("image_url"):
            continue
        hint = event.get("image_hint", "")
        if not hint:
            continue
        # Use the venue id as the image filename so subsequent runs are cacheable
        vid = v.get("id") or v.get("name", "venue").lower().replace(" ", "-")
        url = _generate_one(f"venue-{vid}", hint)
        if url:
            event["image_url"] = url
            count += 1
    return count
