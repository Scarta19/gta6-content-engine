"""`gce` command line interface. Every external side effect is opt-in."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer

from gce import (
    analytics,
    assets,
    discovery,
    gates,
    higgsfield,
    publish,
    render,
    review,
    scripting,
    verify,
)
from gce import config as config_mod
from gce import db as db_mod
from gce import zernio as zernio_mod

app = typer.Typer(no_args_is_help=True, help="GTA VI content engine.")
script_app = typer.Typer(no_args_is_help=True, help="Create, import and check scripts.")
asset_app = typer.Typer(no_args_is_help=True, help="Register media with provenance.")
metrics_app = typer.Typer(no_args_is_help=True, help="Capture performance metrics.")
app.add_typer(script_app, name="script")
app.add_typer(asset_app, name="asset")
app.add_typer(metrics_app, name="metrics")

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
def discover() -> None:
    """Fetch feeds, then cluster and grade new items into stories."""
    c = cfg()
    with db_mod.session(c.paths.db) as conn:
        echo_json(discovery.run(conn, c.discovery, c.verification))
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
    fmt: Annotated[str, typer.Argument(help="news | analysis | concept")],
    story: Annotated[int | None, typer.Option(help="Story id (required for news)")] = None,
) -> None:
    """Generate a script with the LLM (costs API tokens)."""
    if fmt not in scripting.FORMATS:
        raise typer.BadParameter(f"format must be one of {scripting.FORMATS}")
    c = cfg()
    with db_mod.session(c.paths.db) as conn:
        script_id, problems = scripting.generate(conn, c.llm, fmt, story)
        typer.echo(scripting.to_json(scripting.load(conn, script_id)))
    _report_problems(script_id, problems)


@script_app.command("import")
def script_import(
    path: Path,
    fmt: Annotated[str, typer.Option("--format", help="news | analysis | concept")],
    story: int | None = None,
) -> None:
    """Import a hand-written (or Claude Code-written) script JSON. No API cost."""
    data = json.loads(path.read_text(encoding="utf-8"))
    with db_mod.session(cfg().paths.db) as conn:
        script_id = scripting.save(
            conn, scripting.Script.from_dict(data, fmt, story), model="manual"
        )
        problems = scripting.check(conn, script_id)
    _report_problems(script_id, problems)


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
) -> None:
    """Render the 9:16 MP4 with voiceover and burned-in captions."""
    c = cfg()
    with db_mod.session(c.paths.db) as conn:
        res = render.render(conn, c, script_id, force=force)
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
    platforms: Annotated[str, typer.Option(help="Comma list")] = "tiktok,instagram,facebook",
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
