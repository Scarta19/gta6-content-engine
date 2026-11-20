"""`gce` command line interface. Every external side effect is opt-in."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer

from gce import (
    analytics,
    assets,
    bounties,
    clipping,
    discovery,
    gates,
    higgsfield,
    publish,
    render,
    review,
    scripting,
    trends,
    verify,
)
from gce import config as config_mod
from gce import db as db_mod
from gce import zernio as zernio_mod

app = typer.Typer(no_args_is_help=True, help="GTA VI content engine.")
script_app = typer.Typer(no_args_is_help=True, help="Create, import and check scripts.")
asset_app = typer.Typer(no_args_is_help=True, help="Register media with provenance.")
metrics_app = typer.Typer(no_args_is_help=True, help="Capture performance metrics.")
trends_app = typer.Typer(no_args_is_help=True, help="Learn topics/formats from viral channels.")
app.add_typer(trends_app, name="trends")
app.add_typer(script_app, name="script")
app.add_typer(asset_app, name="asset")
app.add_typer(metrics_app, name="metrics")
clip_app = typer.Typer(
    no_args_is_help=True,
    help="Clipping mode for paid clip campaigns (Content Rewards): authorized footage only.",
)
app.add_typer(clip_app, name="clip")
bounty_app = typer.Typer(no_args_is_help=True, help="Watch clipping platforms for campaigns.")
app.add_typer(bounty_app, name="bounties")

_state: dict[str, Any] = {}


@app.callback()
def main(
    config: Annotated[Path | None, typer.Option(help="Path to config TOML.")] = None,
) -> None:
    _state["config_path"] = config


def cfg() -> config_mod.Config:
    if "config" not in _state:
        _state["config"] = config_mod.load(_state.get("config_path"))
    c: config_mod.Config = _state["config"]
    return c


def echo_json(data: Any) -> None:
    typer.echo(json.dumps(data, indent=2, default=str))


def table(rows: list[dict[str, Any]]) -> None:
    if not rows:
        typer.echo("(none)")
        return
    cols = list(rows[0])
    widths = {c: max(len(c), *(len(str(r[c])) for r in rows)) for c in cols}
    typer.echo("  ".join(c.ljust(widths[c]) for c in cols))
    for r in rows:
        typer.echo("  ".join(str(r[c]).ljust(widths[c]) for c in cols))


# --- discovery & verification -------------------------------------------------------------


@app.command()
def init() -> None:
    """Create the SQLite database."""
    with db_mod.session(cfg().paths.db):
        pass
    typer.echo(f"database ready at {cfg().paths.db}")


@app.command()
def discover(
    bodies: Annotated[
        int, typer.Option(help="Also fetch article text for up to N official/reputable items")
    ] = 0,
) -> None:
    """Fetch feeds, then cluster and grade new items into stories."""
    c = cfg()
    with db_mod.session(c.paths.db) as conn:
        echo_json(discovery.run(conn, c.discovery, c.verification))
        if bodies:
            echo_json({"article_text": discovery.fetch_bodies(conn, bodies)})
        new = verify.run(conn)
        typer.echo(f"{len(new)} new stories")
    stories()


@app.command()
def stories(limit: int = 15) -> None:
    """Unused stories ranked by score (confirmed > corroborated > unverified)."""
    with db_mod.session(cfg().paths.db) as conn:
        rows = verify.top_stories(conn, limit)
        table(
            [
                {
                    "id": r["id"],
                    "status": r["status"],
                    "score": r["score"],
                    "srcs": r["official_count"] + r["reputable_count"] + r["other_count"],
                    "title": r["title"][:80],
                }
                for r in rows
            ]
        )


@app.command()
def sources(story_id: int) -> None:
    """Show every source behind a story with its tier."""
    with db_mod.session(cfg().paths.db) as conn:
        table(
            [
                {
                    "tier": r["tier"],
                    "domain": r["domain"],
                    "title": r["title"][:70],
                    "url": r["url"],
                }
                for r in verify.sources_for(conn, story_id)
            ]
        )


# --- scripts -------------------------------------------------------------------------------


@script_app.command("new")
def script_new(
    fmt: Annotated[str, typer.Argument(help=" | ".join(scripting.FORMATS))],
    story: Annotated[int | None, typer.Option(help="Story id (required for news)")] = None,
    lang: Annotated[str, typer.Option(help="en | es | pt | fr")] = "en",
    topic: Annotated[str, typer.Option(help="Topic for curiosity/guide/mystery")] = "",
    source: Annotated[
        list[str] | None, typer.Option(help="Citable source URL (repeatable)")
    ] = None,
    from_trend: Annotated[
        str | None, typer.Option(help="Trend video id: use its topic/format as a signal")
    ] = None,
    research: Annotated[
        str,
        typer.Option(
            help="Pull citable articles whose text matches these words (gce discover --bodies)"
        ),
    ] = "",
) -> None:
    """Generate a script with the LLM (costs API tokens)."""
    if fmt not in scripting.FORMATS:
        raise typer.BadParameter(f"format must be one of {scripting.FORMATS}")
    if lang not in scripting.LANGUAGES:
        raise typer.BadParameter(f"lang must be one of {list(scripting.LANGUAGES)}")
    c = cfg()
    with db_mod.session(c.paths.db) as conn:
        trend = None
        if from_trend:
            trend = conn.execute(
                "SELECT * FROM trend_videos WHERE video_id = ?", (from_trend,)
            ).fetchone()
            if trend is None:
                raise typer.BadParameter(f"trend video {from_trend} not found; run gce trends pull")
            pats = ", ".join(trends.patterns_of(trend["title"])) or "none detected"
            topic = (
                f"{topic} Make an ORIGINAL video on a related topic. A viral video titled "
                f"{trend['title']!r} used these hook formats: {pats}. Use the format idea, "
                "never its wording, footage or specific claims unless our sources confirm them."
            ).strip()
        script_id, problems = scripting.generate(
            conn,
            c.llm,
            fmt,
            story,
            language=lang,
            topic=topic,
            extra_sources=source,
            research=research,
        )
        if trend is not None:
            trends.log_inspiration(conn, script_id, trend)
        typer.echo(scripting.to_json(scripting.load(conn, script_id)))
    _report_problems(script_id, problems)


@script_app.command("import")
def script_import(
    path: Path,
    fmt: Annotated[str, typer.Option("--format", help=" | ".join(scripting.FORMATS))],
    story: int | None = None,
    lang: Annotated[str, typer.Option(help="en | es | pt | fr")] = "en",
) -> None:
    """Import a hand-written (or Claude Code-written) script JSON. No API cost."""
    data = json.loads(path.read_text(encoding="utf-8"))
    with db_mod.session(cfg().paths.db) as conn:
        script_id = scripting.save(
            conn, scripting.Script.from_dict(data, fmt, story, lang), model="manual"
        )
        problems = scripting.check(conn, script_id)
    _report_problems(script_id, problems)


@script_app.command("inspire")
def script_inspire(
    script_id: int,
    url: str,
    creator: str = "",
    platform: str = "",
    note: Annotated[str, typer.Option(help="What we took: topic, format, pacing")] = "",
) -> None:
    """Log a third-party video that inspired a script. Ideas only: its media is never used."""
    with db_mod.session(cfg().paths.db) as conn:
        conn.execute(
            "INSERT INTO inspirations (script_id, url, creator, platform, note, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (script_id, url, creator, platform, note, db_mod.now()),
        )
    typer.echo(f"inspiration logged for script {script_id}")


@script_app.command("show")
def script_show(script_id: int) -> None:
    with db_mod.session(cfg().paths.db) as conn:
        typer.echo(scripting.to_json(scripting.load(conn, script_id)))


@script_app.command("check")
def script_check(script_id: int) -> None:
    with db_mod.session(cfg().paths.db) as conn:
        _report_problems(script_id, scripting.check(conn, script_id))


def _report_problems(script_id: int, problems: list[str]) -> None:
    if problems:
        typer.secho(f"script {script_id} has {len(problems)} problem(s):", fg="yellow")
        for p in problems:
            typer.echo(f"  - {p}")
    else:
        typer.secho(f"script {script_id} passes automated checks", fg="green")


# --- assets & higgsfield -------------------------------------------------------------------


@asset_app.command("add")
def asset_add(
    path: Path,
    license: Annotated[str, typer.Option(help=" | ".join(assets.LICENSES))],
    source: Annotated[str, typer.Option(help="Who made it / where it came from")],
    provenance_url: str = "",
    notes: str = "",
) -> None:
    with db_mod.session(cfg().paths.db) as conn:
        asset_id = assets.add(conn, path, license, source, provenance_url, notes)
    typer.echo(f"asset {asset_id} registered ({license})")


@asset_app.command("attach")
def asset_attach(script_id: int, asset_ids: list[int]) -> None:
    """Set the ordered visuals for a script."""
    with db_mod.session(cfg().paths.db) as conn:
        assets.attach(conn, script_id, asset_ids)
    typer.echo(f"script {script_id} visuals: {asset_ids}")


@asset_app.command("list")
def asset_list() -> None:
    with db_mod.session(cfg().paths.db) as conn:
        rows = conn.execute("SELECT * FROM assets ORDER BY id").fetchall()
        table(
            [
                {
                    "id": r["id"],
                    "kind": r["kind"],
                    "license": r["license"],
                    "source": r["source"][:30],
                    "path": r["path"],
                }
                for r in rows
            ]
        )


@app.command("higgsfield")
def higgsfield_cmd(
    prompt: Annotated[str, typer.Option()],
    image_url: Annotated[str | None, typer.Option(help="Public image URL (image-to-video)")] = None,
    duration: int = 5,
    resolution: str = "1080p",
    script: Annotated[int | None, typer.Option(help="Script id to charge the cost to")] = None,
    live: Annotated[bool, typer.Option(help="Actually call Higgsfield (spends credits)")] = False,
) -> None:
    """Generate an AI clip. Dry-run unless --live."""
    c = cfg()
    payload: dict[str, Any] = {"prompt": prompt, "duration": duration, "resolution": resolution}
    if image_url:
        payload["image_url"] = image_url
    client = higgsfield.Client(c.higgsfield, live=live)
    with db_mod.session(c.paths.db) as conn:
        echo_json(
            higgsfield.generate_asset(
                conn, client, payload, c.paths.assets_dir / "higgsfield", script
            )
        )


# --- render, review, publish ---------------------------------------------------------------


@app.command("render")
def render_cmd(
    script_id: int,
    force: Annotated[bool, typer.Option(help="Render even if checks fail (draft)")] = False,
    voice_file: Annotated[
        Path | None,
        typer.Option(help="Use this pre-generated narration audio instead of the TTS provider"),
    ] = None,
) -> None:
    """Render the 9:16 MP4 with voiceover and burned-in captions."""
    c = cfg()
    with db_mod.session(c.paths.db) as conn:
        res = render.render(conn, c, script_id, force=force, voice_file=voice_file)
    typer.echo(
        f"render {res.render_id}: {res.path} ({res.duration_s:.1f}s, tts ${res.tts_usd:.4f})"
    )


@app.command("gates")
def gates_cmd(
    render_id: int,
    llm: Annotated[
        bool, typer.Option(help="Also score the hook with the LLM (+2 alternatives)")
    ] = False,
) -> None:
    """Run the facts, rights/safety and virality gates. Exit code 1 if any fails."""
    c = cfg()
    with db_mod.session(c.paths.db) as conn:
        report = gates.run(conn, c, render_id, use_llm=llm)
    _print_gates(report)
    if not report.passed:
        raise typer.Exit(1)


def _print_gates(report: gates.GateReport) -> None:
    for g in report.gates:
        typer.secho(
            f"[{'PASS' if g.passed else 'FAIL'}] {g.name}", fg="green" if g.passed else "red"
        )
        for issue in g.issues:
            typer.echo(f"    - {issue}")
        for k, v in g.info.items():
            typer.echo(f"    {k}: {json.dumps(v)}")


@app.command("review")
def review_cmd(
    render_id: int,
    reviewer: Annotated[str, typer.Option(prompt="Reviewer name")],
    notes: str = "",
    llm: Annotated[bool, typer.Option(help="LLM hook scoring in the gates")] = False,
) -> None:
    """Run quality gates, then the interactive human approval checklist."""
    c = cfg()
    with db_mod.session(c.paths.db) as conn:
        report = gates.run(conn, c, render_id, use_llm=llm)
        _print_gates(report)
        if not report.passed:
            typer.secho("quality gates failed: fix the script/assets and re-render", fg="red")
            raise typer.Exit(1)
        echo_json(review.summary(conn, render_id))
        answers = {key: typer.confirm(text, default=False) for key, text in review.CHECKLIST}
        decision = review.decide(conn, render_id, reviewer, answers, notes)
    typer.secho(f"render {render_id}: {decision}", fg="green" if decision == "approved" else "red")


@app.command("export")
def export_cmd(render_id: int, dest: Path = Path("out/exports")) -> None:
    """Package a render for manual upload (video, caption, captions, rights sheet)."""
    with db_mod.session(cfg().paths.db) as conn:
        typer.echo(str(publish.export(conn, render_id, dest)))


@app.command("publish")
def publish_cmd(
    render_id: int,
    platforms: Annotated[str, typer.Option(help="Comma list")] = (
        "tiktok,instagram,facebook,youtube"
    ),
    live: Annotated[bool, typer.Option(help="Really post via Zernio")] = False,
    schedule: Annotated[str | None, typer.Option(help="e.g. 2026-11-20T18:00:00")] = None,
    tiktok_privacy: str = "PUBLIC_TO_EVERYONE",
    ai_disclosure_confirmed: bool = False,
) -> None:
    """Publish via Zernio. Dry-run (prints the request) unless --live."""
    c = cfg()
    with db_mod.session(c.paths.db) as conn:
        echo_json(
            publish.publish(
                conn,
                c,
                render_id,
                [p.strip() for p in platforms.split(",") if p.strip()],
                live=live,
                scheduled_for=schedule,
                tiktok_privacy=tiktok_privacy,
                ai_disclosure_confirmed=ai_disclosure_confirmed,
            )
        )


@app.command("record-post")
def record_post(render_id: int, platform: str, external_id: str) -> None:
    """Record a manually uploaded post (platform post id or Zernio id)."""
    with db_mod.session(cfg().paths.db) as conn:
        pub_id = publish.record_manual(conn, render_id, platform, external_id)
    typer.echo(f"publication {pub_id} recorded")


@app.command("zernio-accounts")
def zernio_accounts() -> None:
    """List connected Zernio accounts (read-only; needs ZERNIO_API_KEY)."""
    echo_json(zernio_mod.Client(cfg().zernio).accounts())


# --- trends ---


@trends_app.command("pull")
def trends_pull() -> None:
    """Fetch recent uploads + stats for the configured channels (YouTube Data API)."""
    c = cfg()
    with db_mod.session(c.paths.db) as conn:
        echo_json(trends.pull(conn, c.trends.channels, c.trends.videos_per_channel))


@trends_app.command("top")
def trends_top(limit: int = 20, days: int | None = None) -> None:
    """Shorts ranked by outlier score (views/hour vs. their channel's median)."""
    c = cfg()
    with db_mod.session(c.paths.db) as conn:
        rows = trends.ranked(conn, days or c.trends.lookback_days)[:limit]
    table(
        [
            {
                "id": r["video_id"],
                "outlier": r["outlier"],
                "vph": r["vph"],
                "views": r["views"],
                "channel": r["channel_title"][:18],
                "patterns": ",".join(r["patterns"]),
                "title": r["title"][:60],
            }
            for r in rows
        ]
    )


@trends_app.command("signals")
def trends_signals(days: int | None = None) -> None:
    """Which hook formats and topics over-perform right now."""
    c = cfg()
    with db_mod.session(c.paths.db) as conn:
        echo_json(trends.signals(trends.ranked(conn, days or c.trends.lookback_days)))


# --- metrics -------------------------------------------------------------------------------


@metrics_app.command("sync")
def metrics_sync() -> None:
    """Pull post analytics from Zernio for live/manual publications."""
    c = cfg()
    with db_mod.session(c.paths.db) as conn:
        typer.echo(f"{analytics.sync(conn, c)} metric snapshots stored")


@metrics_app.command("add")
def metrics_add(
    publication_id: int,
    views: int = 0,
    likes: int = 0,
    comments: int = 0,
    shares: int = 0,
    saves: int = 0,
) -> None:
    """Enter metrics by hand (from the TikTok/Instagram/Facebook app)."""
    with db_mod.session(cfg().paths.db) as conn:
        analytics.add(
            conn,
            publication_id,
            {
                "views": views,
                "likes": likes,
                "comments": comments,
                "shares": shares,
                "saves": saves,
            },
        )
    typer.echo("stored")


@app.command("report")
def report_cmd() -> None:
    """Cost per video and performance."""
    c = cfg()
    with db_mod.session(c.paths.db) as conn:
        table(analytics.report(conn, c))


# --- clipping mode ---------------------------------------------------------------------------


@clip_app.command("campaign-add")
def clip_campaign_add(
    name: str,
    rate: Annotated[float, typer.Option(help="USD per 1,000 views (rate campaigns)")] = 0.0,
    pool: Annotated[float, typer.Option(help="Prize pool in USD split by views")] = 0.0,
    min_views: Annotated[int, typer.Option(help="Views a post needs to count")] = 0,
    url: Annotated[str, typer.Option(help="Campaign page URL")] = "",
    min_payout: Annotated[float, typer.Option(help="Min USD before a post pays")] = 0.0,
    max_payout: Annotated[float, typer.Option(help="Per-post cap in USD (0 = none)")] = 0.0,
    fee_pct: Annotated[float, typer.Option(help="Platform fee on payouts, %")] = 7.0,
    platforms: Annotated[str, typer.Option(help="Space-separated")] = "tiktok youtube instagram",
    rules: Annotated[str, typer.Option(help="Campaign rules, pasted from the campaign page")] = "",
    required_text: Annotated[str, typer.Option(help="Tags/mentions required in captions")] = "",
    watermark: Annotated[str, typer.Option(help="Handle of the dedicated clip account")] = "",
) -> None:
    """Register a campaign and its payout rules."""
    with db_mod.session(cfg().paths.db) as conn:
        cid = clipping.add_campaign(
            conn,
            name,
            rate,
            url=url,
            min_payout=min_payout,
            max_payout=max_payout,
            fee_pct=fee_pct,
            platforms=platforms,
            rules=rules,
            required_text=required_text,
            watermark=watermark,
            pool_usd=pool,
            min_views=min_views,
        )
    typer.echo(f"campaign {cid}: {name}")


@clip_app.command("overlay")
def clip_overlay(
    campaign: str,
    path: Annotated[Path | None, typer.Argument(help="PNG overlay; omit to clear")] = None,
) -> None:
    """Set the campaign-provided overlay every clip must carry (e.g. the streamer's frame)."""
    with db_mod.session(cfg().paths.db) as conn:
        clipping.set_overlay(conn, clipping.campaign(conn, campaign)["id"], path)
    typer.echo("overlay set" if path else "overlay cleared")


@clip_app.command("campaigns")
def clip_campaigns() -> None:
    with db_mod.session(cfg().paths.db) as conn:
        table(
            [
                {k: r[k] for k in ("id", "name", "rate_per_1k", "min_payout", "max_payout")}
                for r in conn.execute("SELECT * FROM campaigns ORDER BY id")
            ]
        )


@clip_app.command("source-add")
def clip_source_add(
    campaign: str,
    source_url: Annotated[str, typer.Option(help="The campaign's link to this footage")],
    path: Annotated[
        Path | None, typer.Option(help="Local file (else downloads with yt-dlp)")
    ] = None,
    authorized: Annotated[
        bool, typer.Option(help="I checked the campaign provides/authorizes this footage")
    ] = False,
) -> None:
    """Add campaign-provided footage."""
    c = cfg()
    with db_mod.session(c.paths.db) as conn:
        camp = clipping.campaign(conn, campaign)
        if not authorized:
            raise typer.BadParameter("pass --authorized once you've confirmed the campaign source")
        local = path or clipping.download(source_url, c.paths.work_dir / "clip_sources")
        sid = clipping.add_source(conn, camp["id"], local, source_url, authorized)
    typer.echo(f"source {sid}: {local}")


@clip_app.command("transcribe")
def clip_transcribe(
    source_id: int,
    model: Annotated[str, typer.Option(help="faster-whisper model size")] = "small",
    lang: Annotated[str | None, typer.Option(help="Force language, e.g. en")] = None,
) -> None:
    """Local word-level transcript (faster-whisper, no API cost)."""
    with db_mod.session(cfg().paths.db) as conn:
        n = clipping.transcribe(conn, source_id, model, lang)
    typer.echo(f"{n} words")


@clip_app.command("find")
def clip_find(
    source_id: int,
    n: int = 5,
    min_s: float = 20.0,
    max_s: float = 60.0,
) -> None:
    """Let the LLM pick the most viral moments (costs API tokens)."""
    c = cfg()
    with db_mod.session(c.paths.db) as conn:
        ids = clipping.find_moments(conn, c.llm, source_id, n, min_s, max_s)
        table(
            [
                dict(r)
                for r in conn.execute(
                    f"SELECT id, start_s, end_s, score, hook FROM clips WHERE id IN"
                    f" ({','.join('?' * len(ids))}) ORDER BY score DESC",
                    ids,
                )
            ]
            if ids
            else []
        )


@clip_app.command("add")
def clip_add(source_id: int, start: float, end: float, hook: str = "", title: str = "") -> None:
    """Add a clip by hand (no API cost)."""
    with db_mod.session(cfg().paths.db) as conn:
        cid = clipping.save_clip(conn, source_id, start, end, hook, title)
    typer.echo(f"clip {cid}")


@clip_app.command("render")
def clip_render(clip_id: int) -> None:
    """Cut a 9:16 clip with blurred fill, word-timed captions and the hook."""
    c = cfg()
    with db_mod.session(c.paths.db) as conn:
        typer.echo(str(clipping.render_clip(conn, c, clip_id)))


@clip_app.command("thumbnail")
def clip_thumbnail(
    clip_id: int,
    text: Annotated[str, typer.Option(help="Big cover text; must match what happens")],
    at: Annotated[float | None, typer.Option(help="Seconds into the clip for the frame")] = None,
    face: Annotated[
        str | None, typer.Option(help="Facecam region x,y,w,h as source fractions, e.g. 0,0,.25,.3")
    ] = None,
) -> None:
    """Make a vertical cover image (cover.jpg) from a real frame of the clip."""
    box = None
    if face:
        parts = [float(v) for v in face.split(",")]
        if len(parts) != 4:
            raise typer.BadParameter("--face needs 4 comma-separated numbers")
        box = (parts[0], parts[1], parts[2], parts[3])
    c = cfg()
    with db_mod.session(c.paths.db) as conn:
        typer.echo(str(clipping.thumbnail(conn, c, clip_id, text, at, box)))


@clip_app.command("review")
def clip_review(
    clip_id: int,
    reviewer: Annotated[str, typer.Option(help="Your name")],
    approve: Annotated[bool, typer.Option("--approve/--reject")] = False,
) -> None:
    """Approve or reject a rendered clip after watching it (shows the campaign rules)."""
    with db_mod.session(cfg().paths.db) as conn:
        rules = conn.execute(
            "SELECT c.rules FROM campaigns c JOIN clip_sources s ON s.campaign_id = c.id"
            " JOIN clips k ON k.source_id = s.id WHERE k.id = ?",
            (clip_id,),
        ).fetchone()
        typer.echo(f"Campaign rules: {rules['rules'] if rules else '?'}")
        typer.echo(clipping.decide(conn, clip_id, reviewer, approve))


@clip_app.command("post")
def clip_post(
    clip_id: int,
    platform: str,
    url: str,
    submitted: Annotated[bool, typer.Option(help="Link already submitted on the campaign")] = False,
) -> None:
    """Record where you posted an approved clip (post and submit manually)."""
    with db_mod.session(cfg().paths.db) as conn:
        typer.echo(f"post {clipping.record_post(conn, clip_id, platform, url, submitted)}")


@clip_app.command("publish")
def clip_publish(
    clip_id: int,
    platforms: Annotated[list[str], typer.Option("--platform", "-p")],
    live: Annotated[bool, typer.Option(help="Actually post (needs approval + env flag)")] = False,
    at: Annotated[str | None, typer.Option(help="Schedule, ISO local time")] = None,
) -> None:
    """Post an approved clip via Zernio (dry run unless --live). Submit on the campaign after."""
    c = cfg()
    with db_mod.session(c.paths.db) as conn:
        out = clipping.publish_clip(conn, c, clip_id, platforms, live=live, scheduled_for=at)
    typer.echo(json.dumps(out, indent=2))
    if out["mode"] == "live":
        typer.echo("Now submit each link on the campaign page within 30 minutes.")


@clip_app.command("unsubmitted")
def clip_unsubmitted() -> None:
    """List posts still waiting to be submitted on their campaign."""
    with db_mod.session(cfg().paths.db) as conn:
        for r in clipping.unsubmitted(conn):
            typer.echo(
                f"post {r['id']} clip {r['clip_id']} {r['platform']} {r['url']} {r['created_at']}"
            )


@clip_app.command("views")
def clip_views(
    post_id: int,
    views: int,
    submitted: Annotated[bool | None, typer.Option("--submitted/--not-submitted")] = None,
) -> None:
    """Update a post's view count (from the campaign dashboard)."""
    with db_mod.session(cfg().paths.db) as conn:
        clipping.set_views(conn, post_id, views, submitted)


@clip_app.command("report")
def clip_report() -> None:
    """Views and estimated payouts per campaign (after the platform fee)."""
    with db_mod.session(cfg().paths.db) as conn:
        echo_json(clipping.report(conn))


# --- bounty intelligence ---------------------------------------------------------------------


@bounty_app.command("scan")
def bounties_scan(
    big_pool: Annotated[float, typer.Option(help="Alert threshold for budget/pool, USD")] = 10_000,
    alerts_file: Annotated[
        Path | None, typer.Option(help="Write unsent alerts here (markdown) and mark them sent")
    ] = None,
) -> None:
    """One monitoring pass (run hourly). Never registers or submits anything."""
    with db_mod.session(cfg().paths.db) as conn:
        echo_json(bounties.scan(conn, big_pool_usd=big_pool))
        rows = bounties.pending_alerts(conn)
        text = "\n\n".join(bounties.format_alert(r) for r in rows)
        if text:
            typer.echo(text)
        if alerts_file is not None:
            alerts_file.write_text(text, encoding="utf-8")
            bounties.mark_sent(conn, [r["id"] for r in rows])


@bounty_app.command("top")
def bounties_top(limit: int = 15, all_niches: bool = False) -> None:
    """Best open campaigns by score (gaming only unless --all-niches)."""
    with db_mod.session(cfg().paths.db) as conn:
        table(
            [
                {
                    "score": r["score"],
                    "name": (r["name"] or "")[:40],
                    "type": r["kind"],
                    "rate": r["rate_per_1k"],
                    "max": r["max_payout"],
                    "budget": r["budget_total"] or r["pool_usd"],
                    "spent": r["budget_used"],
                    "creators": r["creators"],
                    "url": r["url"],
                }
                for r in bounties.top(conn, limit, gaming_only=not all_niches)
            ]
        )
