"""Bounty intelligence: watch clipping-campaign platforms for new or changed campaigns.

MVP source: Content Rewards (contentrewards.com). Its robots.txt allows the public campaign
pages, and its sitemap lists every campaign URL with a <lastmod>, so an hourly run only fetches
campaigns that are new or changed. Parsing is done on the page's visible text, and every field
that isn't found stays None (unknown), never guessed.

X (Twitter) is optional: reading posts needs a paid X API plan, and scraping X breaks its terms,
so it isn't used here.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx

from gce import db

USER_AGENT = "gta6-content-engine/0.1 (+campaign monitor; respects robots.txt)"

SCHEMA = """
CREATE TABLE IF NOT EXISTS bounty_campaigns (
    id INTEGER PRIMARY KEY,
    url TEXT NOT NULL UNIQUE,
    source TEXT NOT NULL DEFAULT 'contentrewards',
    lastmod TEXT,
    name TEXT,
    organizer TEXT,
    status TEXT,                      -- accepting | paused | ended | None (unknown)
    kind TEXT,                        -- rate | pool | None
    rate_per_1k REAL,
    min_payout REAL,
    max_payout REAL,
    budget_used REAL,
    budget_total REAL,
    pool_usd REAL,
    min_views INTEGER,
    creators INTEGER,
    platforms TEXT,
    gaming INTEGER NOT NULL DEFAULT 0,
    gta INTEGER NOT NULL DEFAULT 0,
    score REAL NOT NULL DEFAULT 0,
    content_hash TEXT,
    first_seen TEXT NOT NULL,
    last_checked TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bounty_alerts (
    id INTEGER PRIMARY KEY,
    campaign_id INTEGER NOT NULL REFERENCES bounty_campaigns(id),
    kind TEXT NOT NULL,               -- new | changed | big_pool
    detail_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    sent INTEGER NOT NULL DEFAULT 0
);
"""

GAMING_RE = re.compile(
    r"\b(gta|grand theft auto|vice city|gaming|gamer|stream(er|ing)?|twitch|kick|fortnite|"
    r"minecraft|roblox|call of duty|warzone|valorant|nopixel|\brp\b)",
    re.I,
)
GTA_RE = re.compile(r"\b(gta\s*(vi|6)|grand theft auto vi|vice city)\b", re.I)
# Never recommended: cannabis/vape, gambling/betting, and loot-box style "pack opening" offers.
BLOCK_RE = re.compile(
    r"\b(cannabis|weed|thc|cbd|vape|puffco|dispensary|casino|gambl\w*|betting|sportsbook|"
    r"1win|stake\.com|slot machines?|roulette|loot ?box(es)?|pack opening|case opening|\w*packs\b)",
    re.I,
)


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


@dataclass
class Campaign:
    url: str
    name: str | None = None
    organizer: str | None = None
    status: str | None = None
    kind: str | None = None
    rate_per_1k: float | None = None
    min_payout: float | None = None
    max_payout: float | None = None
    budget_used: float | None = None
    budget_total: float | None = None
    pool_usd: float | None = None
    min_views: int | None = None
    creators: int | None = None
    platforms: list[str] = field(default_factory=list)
    gaming: bool = False
    gta: bool = False
    requires_application: bool = False
    description: str = ""
    blocked: bool = False

    def missing(self) -> list[str]:
        # min payout and min views aren't in the listing: they come from the campaign rules.
        return [k for k, v in asdict(self).items() if v is None]


# --- fetching --------------------------------------------------------------------------------

DISCOVER_URLS = (
    "https://contentrewards.com/c/discover/clipping",
    "https://contentrewards.com/c/discover",
)
PUSH_RE = re.compile(r"self\.__next_f\.push\((\[.*?\])\)</script>", re.S)
MARKER = '{"availableBudgetRaw"'


def _rsc_text(html: str) -> str:
    """Concatenate the Next.js flight payload chunks embedded in the page."""
    out: list[str] = []
    for raw in PUSH_RE.findall(html):
        try:
            arr = json.loads(raw)
        except json.JSONDecodeError:
            continue
        out += [x for x in arr if isinstance(x, str)]
    return "".join(out)


def _rate(v: Any) -> float | None:
    """Rate per 1K from labels like "$1", "$1.50" or a range "$0.20–$1" (lowest, conservative)."""
    nums = re.findall(r"\d+(?:\.\d+)?", str(v or "").replace(",", ""))
    return min(float(n) for n in nums) if nums else None


def _num(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace("$", "").replace(",", ""))
    except ValueError:
        return None


def campaign_from(obj: dict[str, Any], now_ms: float | None = None) -> Campaign:
    cid = str(obj.get("id", ""))
    kind = {"cpm": "rate", "pool": "pool"}.get(str(obj.get("type", "")).lower())
    cents = _num(obj.get("maxPayoutCents"))
    pool = _num(obj.get("poolCents"))
    opens = _num(obj.get("opensAt"))
    status = "accepting"
    if opens and now_ms and opens > now_ms:
        status = "upcoming"
    platforms = obj.get("platforms") or []
    c = Campaign(
        url=f"https://contentrewards.com/c/campaigns/{cid}",
        name=obj.get("title") or None,
        organizer=obj.get("brand") or obj.get("organizationProfileHandle") or None,
        status=status,
        kind=kind,
        rate_per_1k=_rate(obj.get("ratePer1kLabel")) if kind == "rate" else None,
        max_payout=cents / 100 if cents is not None else None,
        budget_used=_num(obj.get("budgetSpentRaw")),
        budget_total=_num(obj.get("budgetTotalRaw")),
        pool_usd=pool / 100 if pool is not None else None,
        creators=int(n) if (n := _num(obj.get("creatorCountRaw"))) is not None else None,
        platforms=sorted(str(x).lower() for x in platforms if x),
        requires_application=bool(obj.get("requiresApplication")),
        description=str(obj.get("description") or "")[:600],
    )
    hay = f"{c.name or ''} {c.organizer or ''} {obj.get('category') or ''} {c.description}"
    c.gaming = bool(GAMING_RE.search(hay))
    c.gta = bool(GTA_RE.search(hay))
    c.blocked = bool(BLOCK_RE.search(hay))
    return c


def parse_discover(html: str, now_ms: float | None = None) -> list[Campaign]:
    """Every campaign object in a Content Rewards discover page (deduplicated by id)."""
    text = _rsc_text(html) or html
    dec = json.JSONDecoder()
    seen: dict[str, Campaign] = {}
    groups: dict[str, Campaign] = {}
    i = text.find(MARKER)
    while i != -1:
        try:
            obj, _ = dec.raw_decode(text, i)
        except json.JSONDecodeError:
            i = text.find(MARKER, i + 1)
            continue
        # A grouping object can wrap several campaigns; keep only leaf campaign objects and keep
        # scanning inside it (i + 1), so nested campaigns aren't skipped or mislabeled.
        if isinstance(obj, dict) and obj.get("id") and json.dumps(obj).count(MARKER[1:]) == 1:
            members = (obj.get("group") or {}).get("campaignIds") or []
            if isinstance(members, list) and len(members) > 1:
                # A group card: its numbers are aggregates over several campaigns, and the same
                # card can appear under different member ids. Keep one row per group.
                key = "group:" + ",".join(sorted(str(m) for m in members))
                if key not in groups:
                    groups[key] = _group_from(obj, members, now_ms)
            else:
                seen[str(obj["id"])] = campaign_from(obj, now_ms)
        i = text.find(MARKER, i + 1)
    grouped = {m for g in groups.values() for m in g.description.split("members: ")[-1].split()}
    return [c for cid, c in seen.items() if cid not in grouped] + list(groups.values())


def _group_from(obj: dict[str, Any], members: list[Any], now_ms: float | None) -> Campaign:
    c = campaign_from(obj, now_ms)
    ids = sorted(str(m) for m in members)
    c.url = f"https://contentrewards.com/c/campaigns/{ids[0]}"
    c.name = f"{c.name or 'Campaign'} (group of {len(ids)}; totals across all)"
    c.kind = "group"
    c.rate_per_1k = None
    c.description = (c.description[:400] + f" | members: {' '.join(ids)}").strip()
    return c


# --- scoring ---------------------------------------------------------------------------------


def total_budget(c: Campaign) -> float | None:
    return c.pool_usd if c.kind == "pool" else c.budget_total


def score(c: Campaign) -> float:
    """0-100 heuristic for a beginner clipper. Unknown fields count as neither good nor bad."""
    if c.blocked:
        return 0.0
    s = 0.0
    if c.status == "accepting":
        s += 25
    elif c.status in ("paused", "ended"):
        return 0.0
    s += 20 if c.gta else 10 if c.gaming else 0
    if c.kind == "rate" and c.rate_per_1k:
        s += min(15.0, c.rate_per_1k * 7.5)  # $2/1K -> full marks
        if c.min_payout is not None and c.rate_per_1k:
            views_to_first_dollar = c.min_payout / c.rate_per_1k * 1000
            s += 15 if views_to_first_dollar <= 5000 else 8 if views_to_first_dollar <= 15000 else 0
    elif c.kind in ("pool", "group"):
        s += 5  # payout depends on everyone else's views
        if c.min_views is not None and c.min_views > 100_000:
            s -= 10
    if c.budget_total and c.budget_used is not None:
        left = 1 - c.budget_used / c.budget_total
        s += 15 * max(0.0, left)
    if c.requires_application:
        s -= 5  # extra gate before you can earn
    if c.creators is not None:
        s += 10 if c.creators < 25 else 5 if c.creators < 100 else 0
    return round(max(0.0, min(100.0, s)), 1)


# --- storage & alerts ------------------------------------------------------------------------


def _hash(c: Campaign) -> str:
    return hashlib.sha256(json.dumps(asdict(c), sort_keys=True).encode()).hexdigest()[:16]


def upsert(
    conn: sqlite3.Connection, c: Campaign, lastmod: str | None, big_pool_usd: float
) -> list[str]:
    """Store a parsed campaign; return the alert kinds raised (new, changed, big_pool)."""
    h = _hash(c)
    row = conn.execute("SELECT * FROM bounty_campaigns WHERE url = ?", (c.url,)).fetchone()
    s = score(c)
    values = (
        lastmod,
        c.name,
        c.organizer,
        c.status,
        c.kind,
        c.rate_per_1k,
        c.min_payout,
        c.max_payout,
        c.budget_used,
        c.budget_total,
        c.pool_usd,
        c.min_views,
        c.creators,
        " ".join(c.platforms),
        int(c.gaming),
        int(c.gta),
        s,
        h,
        db.now(),
    )
    cols = (
        "lastmod, name, organizer, status, kind, rate_per_1k, min_payout, max_payout,"
        " budget_used, budget_total, pool_usd, min_views, creators, platforms, gaming, gta,"
        " score, content_hash, last_checked"
    )
    alerts: list[str] = []
    if row is None:
        cur = conn.execute(
            f"INSERT INTO bounty_campaigns (url, first_seen, {cols})"
            f" VALUES (?, ?, {', '.join('?' * len(values))})",
            (c.url, db.now(), *values),
        )
        cid = cur.lastrowid
        alerts.append("new")
    else:
        cid = row["id"]
        sets = ", ".join(f"{col.strip()} = ?" for col in cols.split(","))
        conn.execute(f"UPDATE bounty_campaigns SET {sets} WHERE id = ?", (*values, cid))
        if row["content_hash"] != h and (row["status"] != c.status or row["score"] != s):
            alerts.append("changed")
    budget = total_budget(c)
    if budget is not None and budget >= big_pool_usd and c.status == "accepting":
        already = conn.execute(
            "SELECT 1 FROM bounty_alerts WHERE campaign_id = ? AND kind = 'big_pool'", (cid,)
        ).fetchone()
        if not already:
            alerts.append("big_pool")
    for kind in alerts:
        if c.blocked:
            continue  # stored for completeness, never alerted
        if kind == "new" and not c.gaming:
            continue  # only gaming campaigns raise "new" alerts; everything is still stored
        conn.execute(
            "INSERT INTO bounty_alerts (campaign_id, kind, detail_json, created_at)"
            " VALUES (?, ?, ?, ?)",
            (cid, kind, json.dumps({**asdict(c), "score": s, "missing": c.missing()}), db.now()),
        )
    return alerts


def scan(
    conn: sqlite3.Connection, client: httpx.Client | None = None, big_pool_usd: float = 10_000
) -> dict[str, Any]:
    """One monitoring pass over the public discover listings (one or two page fetches)."""
    import time

    own = client is None
    http = client or httpx.Client(
        timeout=30, headers={"User-Agent": USER_AGENT}, follow_redirects=True
    )
    report: dict[str, Any] = {"pages": {}, "campaigns": 0, "alerts": 0}
    found: dict[str, Campaign] = {}
    try:
        for url in DISCOVER_URLS:
            try:
                resp = http.get(url)
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                report["pages"][url] = f"error: {exc}"
                continue
            camps = parse_discover(resp.text, time.time() * 1000)
            report["pages"][url] = len(camps)
            found.update({c.url: c for c in camps})
    finally:
        if own:
            http.close()
    report["campaigns"] = len(found)
    for c in found.values():
        report["alerts"] += len(upsert(conn, c, None, big_pool_usd))
    if found:
        # Listed before but gone now: no longer open (budget spent or closed).
        gone = conn.execute(
            "SELECT id, url FROM bounty_campaigns WHERE status IN ('accepting', 'upcoming')"
        ).fetchall()
        for r in gone:
            if r["url"] not in found:
                conn.execute(
                    "UPDATE bounty_campaigns SET status = 'closed', last_checked = ? WHERE id = ?",
                    (db.now(), r["id"]),
                )
    return report


def pending_alerts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT a.*, c.url, c.name, c.score FROM bounty_alerts a"
        " JOIN bounty_campaigns c ON c.id = a.campaign_id WHERE a.sent = 0 ORDER BY c.score DESC"
    ).fetchall()


def format_alert(row: sqlite3.Row) -> str:
    d = json.loads(row["detail_json"])

    def money(v: float | None) -> str:
        return "unknown" if v is None else f"${v:,.2f}"

    lines = [
        f"[{row['kind'].upper()}] {d.get('name') or '(unnamed)'} - score {row['score']}",
        f"  {row['url']}",
        f"  organizer: {d.get('organizer') or 'unknown'} | status: {d.get('status') or 'unknown'}"
        f" | type: {d.get('kind') or 'unknown'}",
        f"  rate/1K: {money(d.get('rate_per_1k'))} | min: {money(d.get('min_payout'))}"
        f" | max: {money(d.get('max_payout'))}",
        f"  budget: {money(d.get('budget_used'))} used of {money(d.get('budget_total'))}"
        f" | pool: {money(d.get('pool_usd'))} | creators: {d.get('creators') or 'unknown'}",
        f"  platforms: {', '.join(d.get('platforms') or []) or 'unknown'}",
        f"  unverified/missing: {', '.join(d.get('missing') or []) or 'none'}",
    ]
    return "\n".join(lines)


def mark_sent(conn: sqlite3.Connection, ids: list[int]) -> None:
    conn.executemany("UPDATE bounty_alerts SET sent = 1 WHERE id = ?", [(i,) for i in ids])


def top(conn: sqlite3.Connection, limit: int = 15, gaming_only: bool = True) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM bounty_campaigns WHERE status = 'accepting'"
        + (" AND gaming = 1" if gaming_only else "")
        + " ORDER BY score DESC LIMIT ?",
        (limit,),
    ).fetchall()
