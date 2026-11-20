from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from gce import scripting
from tests.conftest import NEWS_SCRIPT


def make(fmt: str = "news", **over: Any) -> scripting.Script:
    return scripting.Script.from_dict({**NEWS_SCRIPT, **over}, fmt, None)


URLS = {"https://www.rockstargames.com/newswire/article/1"}


def test_valid_news_passes() -> None:
    assert scripting.validate(make(), "confirmed", URLS) == []


def test_news_needs_verified_story() -> None:
    problems = scripting.validate(make(), "unverified", URLS)
    assert any("confirmed or corroborated" in p for p in problems)


def test_rejects_unknown_source_and_long_hook() -> None:
    s = make(hook=" ".join(["word"] * 13))
    s.claims = [{"claim": "x", "source_url": "https://random.blog/x", "status": "confirmed"}]
    problems = scripting.validate(s, "confirmed", URLS)
    assert any("hook" in p for p in problems)
    assert any("not in the story sources" in p for p in problems)


def test_blocks_false_confirmation_language() -> None:
    s = make(voiceover=NEWS_SCRIPT["voiceover"] + " A leak confirms the map size.")
    assert any("not confirmed" in p for p in scripting.validate(s, "corroborated", URLS))


def test_concept_needs_no_on_screen_disclosure() -> None:
    # Disclosure moved to the platform AI flags set at publish time.
    s = make("concept", claims=[])
    assert scripting.validate(s, None, set()) == []


def test_extract_json_from_chatty_reply() -> None:
    assert scripting.extract_json('Sure!\n```json\n{"a": 1}\n```') == {"a": 1}
    with pytest.raises(ValueError):
        scripting.extract_json("no json here")


def test_missing_fields() -> None:
    with pytest.raises(ValueError, match="hook"):
        scripting.Script.from_dict({**NEWS_SCRIPT, "hook": ""}, "news", None)


def test_save_load_roundtrip(conn: sqlite3.Connection) -> None:
    sid = scripting.save(conn, make())
    loaded = scripting.load(conn, sid)
    assert loaded.claims == NEWS_SCRIPT["claims"]
    assert loaded.hashtags == ["gta6", "gtavi", "rockstargames"]
    assert scripting.check(conn, sid) == []  # standalone script: claims all confirmed


def test_source_ids_resolve_to_urls() -> None:
    data = {
        "claims": [
            {"claim": "a", "source_id": "S1", "status": "confirmed"},
            {"claim": "b", "source_id": "[S9]", "status": "confirmed"},
            {"claim": "c", "source_url": "https://x.test", "status": "confirmed"},
        ]
    }
    out = scripting.resolve_source_ids(data, {"S1": "https://rockstargames.com/n/1"})
    urls = [c["source_url"] for c in out["claims"]]
    assert urls == ["https://rockstargames.com/n/1", "unknown-source:S9", "https://x.test"]
    assert all("source_id" not in c for c in out["claims"])
