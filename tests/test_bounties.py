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
