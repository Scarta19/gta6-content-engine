from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from gce import config, db

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def cfg(tmp_path: Path) -> config.Config:
    base = config.load(ROOT / "config.example.toml")
    return replace(
        base,
        paths=config.Paths(
            db=tmp_path / "t.sqlite3", work_dir=tmp_path / "out", assets_dir=tmp_path / "assets"
        ),
        zernio=replace(
            base.zernio, accounts={"tiktok": "a" * 24, "instagram": "b" * 24, "facebook": "c" * 24}
        ),
    )


@pytest.fixture
def conn(cfg: config.Config) -> Iterator[sqlite3.Connection]:
    c = db.connect(cfg.paths.db)
    yield c
    c.close()


NEWS_SCRIPT = {
    "title": "GTA VI trailer 3 is out",
    "hook": "Rockstar just dropped something new for GTA VI.",
    "voiceover": (
        "Rockstar Games has published a new GTA VI trailer on its official Newswire. "
        "Here is what it actually shows, and what it does not. The trailer focuses on Jason and "
        "Lucia in Leonida, with new looks at the city and the coast. Rockstar did not announce "
        "a new release date in the post, so anyone claiming otherwise is guessing. We will "
        "break down every frame in our next video, so stay close."
    ),
    "cta": "Follow so you do not miss the frame-by-frame breakdown.",
    "caption": "New official GTA VI trailer: what it shows and what it does not.",
    "hashtags": ["gta6", "gtavi", "rockstargames"],
    "on_screen": ["Official trailer", "What is new", "What is NOT confirmed"],
    "visual_plan": "Official press screenshots with slow push-ins.",
    "claims": [
        {
            "claim": "Rockstar published a new trailer",
            "source_url": "https://www.rockstargames.com/newswire/article/1",
            "status": "confirmed",
        }
    ],
    "rights_notes": "Official press-kit stills only.",
}
