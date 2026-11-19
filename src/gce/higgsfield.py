"""Higgsfield adapter (https://docs.higgsfield.ai), verified against the public docs:

- Auth header:  Authorization: Key {HF_API_KEY_ID}:{HF_API_KEY_SECRET}
- Submit:       POST {base_url}{model_path}  (fields vary per model; see the model's page)
                optional Idempotency-Key header
- Poll:         GET {base_url}/requests/{request_id}/status
- Terminal:     completed | failed | nsfw | canceled ; video models return video.url
- Outputs are retained ~7 days, so we download immediately.

Dry-run is the default and makes no network calls. Live calls spend credits.
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

from gce import assets, db
from gce.config import HiggsfieldCfg, secret

TERMINAL = {"completed", "failed", "nsfw", "canceled"}


class HiggsfieldError(RuntimeError):
    pass


def _auth_header() -> dict[str, str]:
    key_id, key_secret = secret("HF_API_KEY_ID"), secret("HF_API_KEY_SECRET")
    if not key_id or not key_secret:
        raise HiggsfieldError("HF_API_KEY_ID and HF_API_KEY_SECRET must be set for live calls")
    return {"Authorization": f"Key {key_id}:{key_secret}"}


def output_url(status: dict[str, Any]) -> str:
    video = status.get("video")
    if isinstance(video, dict) and video.get("url"):
        return str(video["url"])
    for key in ("videos", "images"):
        items = status.get(key)
        if isinstance(items, list) and items and isinstance(items[0], dict) and items[0].get("url"):
            return str(items[0]["url"])
    raise HiggsfieldError(f"completed response has no output url: keys={sorted(status)}")


class Client:
    def __init__(
        self, cfg: HiggsfieldCfg, live: bool = False, http: httpx.Client | None = None
    ) -> None:
        self.cfg = cfg
        self.live = live
        self.http = http or httpx.Client(timeout=60)

    def plan(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "method": "POST",
            "url": f"{self.cfg.base_url}{self.cfg.model_path}",
            "json": payload,
            "est_usd": self.cfg.est_usd_per_generation,
        }

    def submit(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.live:
            return {"dry_run": True, **self.plan(payload)}
        if not self.cfg.enabled:
            raise HiggsfieldError("higgsfield.enabled is false in config")
        resp = self.http.post(
            f"{self.cfg.base_url}{self.cfg.model_path}",
            headers={**_auth_header(), "Idempotency-Key": str(uuid.uuid4())},
            json=payload,
        )
        if resp.status_code >= 400:
            raise HiggsfieldError(f"submit failed {resp.status_code}: {resp.text[:500]}")
        data: dict[str, Any] = resp.json()
        return data

    def status(self, request_id: str) -> dict[str, Any]:
        resp = self.http.get(
            f"{self.cfg.base_url}/requests/{request_id}/status", headers=_auth_header()
        )
        if resp.status_code >= 400:
            raise HiggsfieldError(f"status failed {resp.status_code}: {resp.text[:500]}")
        data: dict[str, Any] = resp.json()
        return data

    def wait(self, request_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + self.cfg.poll_timeout_s
        delay = self.cfg.poll_interval_s
        while True:
            st = self.status(request_id)
            if st.get("status") in TERMINAL:
                return st
            if time.monotonic() > deadline:
                raise HiggsfieldError(f"timed out waiting for {request_id}")
            time.sleep(delay)
            delay = min(delay * 1.5, 30)


def generate_asset(
    conn: sqlite3.Connection,
    client: Client,
    payload: dict[str, Any],
    dest_dir: Path,
    script_id: int | None,
) -> dict[str, Any]:
    """Submit, wait, download and register the result as an ai_generated asset."""
    submitted = client.submit(payload)
    if submitted.get("dry_run"):
        return submitted
    request_id = str(submitted["request_id"])
    final = client.wait(request_id)
    if final.get("status") != "completed":
        raise HiggsfieldError(f"generation {request_id} ended as {final.get('status')}")
    url = output_url(final)
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / f"hf_{request_id}{Path(url.split('?')[0]).suffix or '.mp4'}"
    with client.http.stream("GET", url) as resp:
        resp.raise_for_status()
        with target.open("wb") as fh:
            for chunk in resp.iter_bytes():
                fh.write(chunk)
    asset_id = assets.add(
        conn,
        target,
        "ai_generated",
        source=f"Higgsfield {client.cfg.model_path}",
        provenance_url=f"higgsfield:request:{request_id}",
        notes=f"prompt: {payload.get('prompt', '')}",
    )
    db.record_cost(
        conn,
        "higgsfield",
        client.cfg.est_usd_per_generation,
        f"{client.cfg.model_path} {request_id}",
        script_id,
    )
    return {"request_id": request_id, "asset_id": asset_id, "path": str(target)}
