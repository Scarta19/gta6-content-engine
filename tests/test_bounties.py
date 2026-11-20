from __future__ import annotations

import sqlite3
from pathlib import Path

import httpx

from gce import bounties

FIXTURE = (Path(__file__).parent / "fixtures" / "cr_discover.html").read_text()


def test_parse_discover_rsc_payload() -> None:
    camps = {c.name: c for c in bounties.parse_discover(FIXTURE)}
    k = camps["KreekCraft GTA 6 Clipping"]
    assert k.kind == "rate" and k.rate_per_1k == 1.0 and k.max_payout == 300.0
    assert k.budget_total == 6824.6 and k.creators == 32 and k.gta and k.gaming
    assert k.min_payout is None  # not in the listing: stays unknown, never guessed
    pool = camps["GTA 6"]
    assert pool.kind == "pool" and pool.pool_usd == 15000.0
    assert not camps["Skincare UGC"].gaming


def test_scan_alerts_dedupe_and_close(conn: sqlite3.Connection) -> None:
    pages = {"html": FIXTURE}

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=pages["html"])

    client = httpx.Client(transport=httpx.MockTransport(handler))
    first = bounties.scan(conn, client)
    assert first["campaigns"] == 3
    kinds = sorted(r["kind"] for r in bounties.pending_alerts(conn))
    # 2 gaming "new" alerts (UGC is stored silently) + 1 big pool (Reach $15k).
    assert kinds == ["big_pool", "new", "new"]
    bounties.mark_sent(conn, [r["id"] for r in bounties.pending_alerts(conn)])

    bounties.scan(conn, client)
    assert bounties.pending_alerts(conn) == []  # deduplicated

    pages["html"] = FIXTURE.replace("KreekCraft GTA 6 Clipping", "X").replace(
        "2eaa14fb-6927-4136-897e-b096e0d907d6", "cccc"
    )
    bounties.scan(conn, client)
    status = conn.execute(
        "SELECT status FROM bounty_campaigns WHERE url LIKE '%2eaa14fb%'"
    ).fetchone()[0]
    assert status == "closed"


def test_score_prefers_open_gta_cpm() -> None:
    camps = {c.name: c for c in bounties.parse_discover(FIXTURE)}
    assert bounties.score(camps["KreekCraft GTA 6 Clipping"]) > bounties.score(camps["GTA 6"])


def test_rate_ranges_blocklist_and_nested_groups() -> None:
    import json as _json

    leaf = {
        "availableBudgetRaw": 500,
        "id": "leaf-1",
        "title": "$12,500 Streamer GTA VI Marathon Pool",
        "type": "pool",
        "poolCents": 1_250_000,
    }
    group = {"availableBudgetRaw": 0, "id": "group-1", "title": "GTA 6", "campaigns": [leaf]}
    ranged = {
        "availableBudgetRaw": 100,
        "id": "r-1",
        "title": "Gaming clips",
        "type": "cpm",
        "ratePer1kLabel": "$0.20–$1",
    }
    vice = {"availableBudgetRaw": 100, "id": "b-1", "title": "Puffco x Streamer", "type": "cpm"}
    text = "".join(_json.dumps(o) for o in (group, ranged, vice))
    camps = {c.url.rsplit("/", 1)[1]: c for c in bounties.parse_discover(text)}
    assert "group-1" not in camps and camps["leaf-1"].name.startswith("$12,500")
    assert camps["r-1"].rate_per_1k == 0.2
    assert camps["b-1"].blocked and bounties.score(camps["b-1"]) == 0.0


def test_group_cards_collapse_to_one_row() -> None:
    import json as _json

    def card(cid: str) -> dict[str, object]:
        return {
            "availableBudgetRaw": 0,
            "id": cid,
            "title": "GTA 6",
            "type": "pool",
            "budgetTotalRaw": 15000,
            "creatorCountRaw": 781,
            "group": {"campaignIds": ["a", "b", "c"]},
        }

    solo = {"availableBudgetRaw": 1, "id": "b", "title": "Clix pool", "type": "pool"}
    text = "".join(_json.dumps(o) for o in (card("a"), card("c"), solo))
    camps = bounties.parse_discover(text)
    assert len(camps) == 1 and camps[0].kind == "group" and "group of 3" in (camps[0].name or "")
