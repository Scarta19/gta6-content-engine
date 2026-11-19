# gta6-content-engine

Lean, CLI-driven pipeline for verified, rights-reviewed GTA VI short-form videos
(TikTok, Instagram Reels, Facebook Reels, YouTube Shorts). Faceless channels, adult
audience (GTA VI is rated M 17+). Scripts in English, Spanish, Portuguese or French.

```
discover -> verify -> script -> assets -> render (FFmpeg + voice + captions)
         -> quality gates -> human review -> publish (Zernio, dry-run default) -> metrics -> cost report
```

One SQLite file holds everything: sources and their tiers, stories, scripts, asset
provenance, renders, approvals, publications, metrics and costs.

## Setup

Requirements: Python 3.12, [uv](https://docs.astral.sh/uv/), FFmpeg with libass.

```bash
# macOS: plain `ffmpeg` lacks libass (captions); use ffmpeg-full
brew install uv ffmpeg-full
export PATH="/opt/homebrew/opt/ffmpeg-full/bin:$PATH"   # add to ~/.zshrc
# Ubuntu/Debian
sudo apt-get install -y ffmpeg && curl -LsSf https://astral.sh/uv/install.sh | sh

git clone git@github.com:Scarta19/gta6-content-engine.git
cd gta6-content-engine
uv sync
cp config.example.toml config.toml     # edit feeds, costs, account ids
cp .env.example .env                   # add keys only when you need them
set -a; source .env; set +a
uv run gce init
uv run pytest -q
```

No API key is needed to discover news, import a script, render, review, export or dry-run
publish. Keys are only needed for the steps that call a paid or account-linked service.

## Daily workflow

```bash
uv run gce discover                       # fetch feeds, cluster, grade, list top stories
uv run gce sources 12                     # inspect every source behind story 12

# Script: generate with the LLM (needs ANTHROPIC_API_KEY, cost is tracked) ...
uv run gce script new news --story 12
# ... or write it yourself / with Claude Code and import it for $0
uv run gce script import my_script.json --format analysis --story 12
uv run gce script check 1

# Niche formats: curiosity | guide | mystery (plus news | analysis | concept), any language
uv run gce script new guide --lang es --topic "fastest car location" --source https://...
uv run gce script inspire 1 https://youtube.com/... --creator "SomeChannel" --note "list pacing"

# Visuals: every asset needs a license and a source
uv run gce asset add shots/still.png --license owned --source "our capture"
uv run gce asset add kit/screen1.jpg --license press_kit --source "Rockstar press site" \
  --provenance-url "https://..."
uv run gce asset attach 1 1 2             # script 1 uses assets 1, 2 in order

uv run gce render 1                       # out/script_1/gce_1.mp4 (1080x1920, <=58s)
uv run gce gates 1 [--llm]                # facts, rights/safety, virality; exit 1 on failure
uv run gce review 1 --reviewer Santiago   # runs the gates, then the human checklist
uv run gce export 1                       # manual-upload folder: video, caption, srt, review.json

uv run gce publish 1                      # DRY RUN: prints the exact Zernio request, sends nothing
uv run gce record-post 1 tiktok <post id> # if you uploaded by hand
uv run gce metrics sync                   # Zernio analytics (or: gce metrics add <pub id> --views ...)
uv run gce report                         # cost per video, views, shares, $/1k views
```

Try it end to end with the bundled sample (no keys needed):

```bash
ffmpeg -f lavfi -i "gradients=s=1080x1920:c0=0xff3d8b:c1=0x1b1464:speed=0.02" -t 4 bg.mp4
uv run gce asset add bg.mp4 --license owned --source "ffmpeg gradient"
uv run gce script import examples/sample_concept.json --format concept
uv run gce asset attach 1 1 && uv run gce render 1
```

## Safety model

| Guard | Where |
|---|---|
| News scripts need a CONFIRMED (official source) or CORROBORATED (2+ reputable outlets) story | `scripting.validate` |
| Every claim must cite a URL from the story's sources; unverified claims must say "rumor" | `scripting.validate` |
| "Confirmed" / "leak confirms" language blocked unless the story is officially confirmed | `scripting.validate` |
| Concept videos must disclose AI in voiceover and caption; on-screen AI label is burned in | `scripting`, `subtitles` |
| Assets need a license (`owned`, `licensed`, `permitted`, `press_kit`, `ai_generated`) and source; third-party ones need a provenance URL | `assets.add` |
| Render refuses scripts that fail checks and videos over 58 s (Facebook Reels max 60 s) | `render.render` |
| Live publish needs an approved human review, `--live`, and `GCE_ALLOW_LIVE_PUBLISH=yes` | `publish.publish` |
| AI concept videos also need `--ai-disclosure-confirmed` before live publish | `publish.publish` |
| Quality gates must pass before a human can approve (see below) | `gates.run`, `review.decide` |
| Higgsfield is dry-run unless `higgsfield.enabled = true` and `--live` | `higgsfield.Client` |

### Learning from viral channels (`gce trends`)

```bash
uv run gce trends pull       # recent uploads + stats of [trends].channels (YouTube Data API)
uv run gce trends top        # Shorts ranked by outlier score (views/hour vs. channel median)
uv run gce trends signals    # which hook formats and topics over-perform
uv run gce script new curiosity --lang es --from-trend <video id> --source https://...
```

Only public metadata is read (title, publish time, views, likes, comments, duration), at
1 quota unit per call. `--from-trend` passes the format and topic as a signal, tells the
model never to reuse wording, footage or unconfirmed claims, and logs the source video as an
inspiration. Needs `YOUTUBE_API_KEY` (Google Cloud console, enable YouTube Data API v3).

### Inspiration vs. copying

Third-party videos can inspire topics, formats and pacing. Log them with
`gce script inspire`; they show in the review sheet. Their footage, audio and wording are
never used: the rights gate blocks any asset whose source matches a logged inspiration.

### Quality gates (`gce gates`)

1. **Facts**: every claim cites a URL from the story's sources and keeps its status.
2. **Rights and safety**: licensed assets only; nothing that looks Rockstar-owned unless
   `allow_rockstar_assets = true` after legal review; AI visuals disclosed in the caption;
   after `launch_date`, spoiler terms need a "spoiler" warning in the hook or caption;
   kid-directed wording is always blocked (GTA VI is rated M 17+, the audience is adults).
3. **Virality pre-check**: hook of 12 words or fewer on screen at 0.0 s, 20 to 58 s,
   burned-in captions, one clear CTA, and a hook score (heuristic by default; `--llm` asks
   the model for a 0-10 score plus two alternative hooks, with cost tracked).
   Set `min_hook_score` to make the score blocking once we have performance data.

Verification tiers live in `config.toml` (`official_domains`, `reputable_domains`).
Unverified material (Reddit, leakers, aggregators) is never presented as news.

## Integrations (checked against official docs on 2026-11-17)

**Zernio** (`src/gce/zernio.py`): base `https://zernio.com/api/v1`, `Authorization: Bearer`.
`GET /accounts`, `POST /media/presign` then `PUT` to `uploadUrl`, `POST /posts` with
`platforms[{platform, accountId}]`, `mediaItems[{url, type}]`, `publishNow` or
`scheduledFor` + `timezone`. TikTok needs `tiktokSettings` with
`content_preview_confirmed` and `express_consent_given` set to true and a `privacy_level`
the creator allows. Business-app TikTok video direct posts must be public. `GET /analytics?postId=`
returns views, likes, comments, shares, saves. Rate limited (honor `Retry-After`).
Not verified yet: the TikTok AI-content label field and Creator Inbox draft fields, so AI
concept posts need manual label confirmation. Run `gce zernio-accounts` once keys exist, then
put the account ids in `config.toml`.

Per-platform options (`platforms[].platformSpecificData`, checked 2026-11-19): YouTube gets
`title`, `visibility`, `madeForKids: false`, `containsSyntheticMedia` (true for AI videos)
and `categoryId: "20"` (Gaming), with tags at the top level; a vertical video of 3 minutes
or less is a Short. Facebook gets `contentType: "reel"` (9:16, 3 to 60 s, 30 API Reels per
Page per day). Instagram publishes a single video as a Reel.

**Higgsfield** (`src/gce/higgsfield.py`): base `https://api.higgsfield.ai`,
`Authorization: Key {id}:{secret}`, submit to the model path (default
`/wan/v2.6/image-to-video`: `prompt`, `image_url`, `duration` 5/10/15, `resolution`),
poll `GET /requests/{id}/status` until `completed|failed|nsfw|canceled`, result at
`video.url`. Outputs expire after about 7 days, so the adapter downloads immediately and
registers the file as an `ai_generated` asset with its request id as provenance.
Set `est_usd_per_generation` from your console after the first paid test.

**LLM** (`src/gce/llm.py`): Anthropic Messages API via `ANTHROPIC_API_KEY`; model and
per-token prices are in config and every call is written to the `costs` table.
Importing hand-written scripts costs nothing.

**Voice** (`src/gce/tts.py`): `silent` (previews/tests), `say` (macOS voices, free,
per language), `piper` (free, local), or
`openai` (paid per character). Captions are timed from the script text, so no speech
recognition is needed.

## Layout

```
src/gce/
  config.py      TOML config, secrets from env only
  db.py          SQLite schema + cost recorder
  discovery.py   RSS/Atom fetch, relevance filter, source tiering
  verify.py      clustering, CONFIRMED / CORROBORATED / UNVERIFIED, scoring
  scripting.py   LLM script generation, import, validation rules
  assets.py      provenance-enforced asset registry
  tts.py         voiceover providers
  subtitles.py   caption timing, ASS/SRT (hook, beats, AI label)
  render.py      FFmpeg 9:16 render
  review.py      human approval checklist
  higgsfield.py  Higgsfield adapter (dry-run default)
  zernio.py      Zernio adapter
  publish.py     dry-run / live / manual export
  analytics.py   metrics sync and cost report
  cli.py         `gce` command
```

## Development

```bash
uv run ruff check . && uv run mypy src && uv run pytest -q
```

CI runs the same on every push and pull request (`.github/workflows/ci.yml`).
