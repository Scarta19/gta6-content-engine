# gta6-content-engine

Lean, CLI-driven pipeline for verified, rights-reviewed GTA VI short-form videos
(TikTok, Instagram Reels, Facebook Reels), US audience, American English.

```
discover -> verify -> script -> assets -> render (FFmpeg + voice + captions)
         -> human review -> publish (Zernio, dry-run default) -> metrics -> cost report
```

One SQLite file holds everything: sources and their tiers, stories, scripts, asset
provenance, renders, approvals, publications, metrics and costs.

## Setup

Requirements: Python 3.12, [uv](https://docs.astral.sh/uv/), FFmpeg with libass.

```bash
# macOS
brew install uv ffmpeg
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

# Visuals: every asset needs a license and a source
uv run gce asset add shots/still.png --license owned --source "our capture"
uv run gce asset add kit/screen1.jpg --license press_kit --source "Rockstar press site" \
  --provenance-url "https://..."
uv run gce asset attach 1 1 2             # script 1 uses assets 1, 2 in order

uv run gce render 1                       # out/script_1/gce_1.mp4 (1080x1920, <=58s)
uv run gce review 1 --reviewer Santiago   # interactive checklist; approval is required
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
| Higgsfield is dry-run unless `higgsfield.enabled = true` and `--live` | `higgsfield.Client` |

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

**Voice** (`src/gce/tts.py`): `silent` (previews/tests), `piper` (free, local), or
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
