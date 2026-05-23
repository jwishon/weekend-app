"""Kie.ai image generation for cron-produced items.

Pattern:
  1. POST /api/v1/jobs/createTask with model + prompt → returns taskId
  2. GET  /api/v1/jobs/recordInfo?taskId=... until state == "success"
  3. Download the returned image URL, save to /app/var/data/img/<id>.png

Generation per-item is fire-and-poll; we run them in parallel with a small
thread pool to keep total cron time bounded (12 items × ~10s each = ~2min
sequential, ~30s parallel with pool=8).

Failures don't abort the cron — the item just goes out without an image_url
and the template falls back to the gradient placeholder.
"""

from __future__ import annotations

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import httpx

log = logging.getLogger("weekend.images")

KIE_BASE = "https://api.kie.ai"
MODEL = "google/nano-banana"
IMG_SIZE = "16:9"
IMG_DIR = Path("/app/var/data/img")
POLL_TIMEOUT_SEC = 60
POLL_INTERVAL = 2

IMG_URL_PREFIX = "/img"  # served by FastAPI from IMG_DIR


def _api_key() -> str:
    k = os.environ.get("KIE_API_KEY", "")
    if not k:
        raise RuntimeError("KIE_API_KEY env var is not set")
    return k


def _create_task(client: httpx.Client, prompt: str) -> str:
    payload = {
        "model": MODEL,
        "input": {
            "prompt": prompt,
            "image_size": IMG_SIZE,
            "output_format": "png",
        },
    }
    r = client.post(
        f"{KIE_BASE}/api/v1/jobs/createTask",
        headers={"Authorization": f"Bearer {_api_key()}"},
        json=payload,
        timeout=15,
    )
    r.raise_for_status()
    body = r.json()
    task_id = body.get("data", {}).get("taskId") or body.get("taskId")
    if not task_id:
        raise RuntimeError(f"createTask returned no taskId: {body}")
    return task_id


def _poll_task(client: httpx.Client, task_id: str) -> str:
    """Poll until success; return the result image URL. Raises on failure/timeout."""
    deadline = time.time() + POLL_TIMEOUT_SEC
    while time.time() < deadline:
        r = client.get(
            f"{KIE_BASE}/api/v1/jobs/recordInfo",
            headers={"Authorization": f"Bearer {_api_key()}"},
            params={"taskId": task_id},
            timeout=10,
        )
        r.raise_for_status()
        body = r.json()
        data = body.get("data") or {}
        state = (data.get("state") or "").lower()
        if state == "success":
            # resultJson contains a JSON string with the actual URLs
            import json
            result = data.get("resultJson", "{}")
            if isinstance(result, str):
                result = json.loads(result)
            urls = result.get("resultUrls", []) or result.get("urls", [])
            if not urls:
                raise RuntimeError(f"success but no resultUrls: {data}")
            return urls[0]
        if state in ("fail", "failed", "error"):
            raise RuntimeError(f"kie task failed: {data.get('failMsg') or data.get('failCode') or data}")
        time.sleep(POLL_INTERVAL)
    raise TimeoutError(f"Polling exceeded {POLL_TIMEOUT_SEC}s for task {task_id}")


def _download(client: httpx.Client, url: str, dest: Path) -> None:
    r = client.get(url, timeout=20)
    r.raise_for_status()
    dest.write_bytes(r.content)


def generate_one(item_id: str, prompt: str) -> str | None:
    """Generate one image. Returns the URL path the template should use, or
    None on any failure (we don't crash the cron on image issues)."""
    IMG_DIR.mkdir(parents=True, exist_ok=True)
    dest = IMG_DIR / f"{item_id}.png"
    if dest.exists():
        return f"{IMG_URL_PREFIX}/{item_id}.png"  # idempotent: skip if already on disk
    try:
        with httpx.Client() as client:
            task_id = _create_task(client, prompt)
            result_url = _poll_task(client, task_id)
            _download(client, result_url, dest)
        log.info("Generated image for %s (%d bytes)", item_id, dest.stat().st_size)
        return f"{IMG_URL_PREFIX}/{item_id}.png"
    except Exception as e:
        log.warning("Image gen failed for %s: %s", item_id, e)
        # If a partial file got written, clean it up
        try:
            if dest.exists() and dest.stat().st_size < 1000:
                dest.unlink()
        except Exception:
            pass
        return None


def generate_for_items(items: list[dict], max_workers: int = 8) -> list[dict]:
    """Mutates items in place: sets item['image_url'] for any that generate
    successfully. Returns the (same) list for chaining. No-ops if KIE_API_KEY
    is missing — items just lack image_url."""
    if not os.environ.get("KIE_API_KEY"):
        log.warning("KIE_API_KEY not set — skipping image generation")
        return items

    todo = [it for it in items if it.get("image_hint") and not it.get("image_url")]
    if not todo:
        return items

    log.info("Generating %d images in parallel (pool=%d)", len(todo), max_workers)
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(generate_one, it["id"], it["image_hint"]): it for it in todo}
        for f in as_completed(futures):
            item = futures[f]
            url = f.result()
            if url:
                item["image_url"] = url
    return items
