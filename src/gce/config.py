"""Configuration loaded from a TOML file. Secrets come only from environment variables."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATHS = (Path("config.toml"), Path("config.example.toml"))


@dataclass(frozen=True)
class Paths:
    db: Path
    work_dir: Path
    assets_dir: Path


@dataclass(frozen=True)
class DiscoveryCfg:
    feeds: list[str]
    keywords: list[str]
    max_age_days: int = 14


@dataclass(frozen=True)
class VerificationCfg:
    official_domains: list[str]
    reputable_domains: list[str]


@dataclass(frozen=True)
class LlmCfg:
    provider: str = "anthropic"
    model: str = "claude-sonnet-5-5"
    max_tokens: int = 4000
    input_usd_per_mtok: float = 3.0
    output_usd_per_mtok: float = 15.0


@dataclass(frozen=True)
class TtsCfg:
    provider: str = "silent"
    words_per_minute: int = 165
    piper_model: str = ""
    openai_model: str = "gpt-4o-mini-tts"
    openai_voice: str = "onyx"
    openai_usd_per_1k_chars: float = 0.015
    # macOS `say` voices per language (free, local). Check installed voices: say -v '?'
    say_voices: dict[str, str] = field(
        default_factory=lambda: {"en": "Samantha", "es": "Paulina", "pt": "Luciana", "fr": "Thomas"}
    )
    # piper .onnx model per language; falls back to piper_model
    piper_models: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class RenderCfg:
    width: int = 1080
    height: int = 1920
    fps: int = 30
    font: str = "DejaVu Sans"
    font_size: int = 76
    max_words_per_caption: int = 4
    ai_label_text: str = "AI-GENERATED CONCEPT - NOT REAL GAMEPLAY"
    background_color: str = "0x101418"
    max_duration_s: float = 58.0  # Facebook Reels cap is 60 s


@dataclass(frozen=True)
class HiggsfieldCfg:
    enabled: bool = False
    base_url: str = "https://api.higgsfield.ai"
    model_path: str = "/wan/v2.6/image-to-video"
    est_usd_per_generation: float = 0.0
    poll_interval_s: float = 5
    poll_timeout_s: float = 900


@dataclass(frozen=True)
class ZernioCfg:
    base_url: str = "https://zernio.com/api/v1"
    timezone: str = "America/New_York"
    accounts: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class GatesCfg:
    launch_date: str = "2026-11-19"
    spoiler_terms: list[str] = field(
        default_factory=lambda: ["ending", "final mission", "plot twist", "betrayal"]
    )
    min_duration_s: float = 30.0
    max_duration_s: float = 58.0
    allow_rockstar_assets: bool = False
    min_hook_score: float = 0.0


@dataclass(frozen=True)
class TrendsCfg:
    channels: list[str] = field(default_factory=list)
    videos_per_channel: int = 20
    lookback_days: int = 14


@dataclass(frozen=True)
class CostsCfg:
    monthly_fixed_usd: float = 0.0


@dataclass(frozen=True)
class Config:
    paths: Paths
    discovery: DiscoveryCfg
    verification: VerificationCfg
    llm: LlmCfg
    tts: TtsCfg
    render: RenderCfg
    higgsfield: HiggsfieldCfg
    zernio: ZernioCfg
    costs: CostsCfg
    gates: GatesCfg = field(default_factory=GatesCfg)
    trends: TrendsCfg = field(default_factory=TrendsCfg)
    source: Path | None = None


def _section(raw: dict[str, Any], name: str) -> dict[str, Any]:
    value = raw.get(name, {})
    if not isinstance(value, dict):
        raise ValueError(f"config section [{name}] must be a table")
    return value


def from_dict(raw: dict[str, Any], source: Path | None = None) -> Config:
    p = _section(raw, "paths")
    d = _section(raw, "discovery")
    v = _section(raw, "verification")
    return Config(
        paths=Paths(
            db=Path(p.get("db", "data/gce.sqlite3")),
            work_dir=Path(p.get("work_dir", "out")),
            assets_dir=Path(p.get("assets_dir", "assets")),
        ),
        discovery=DiscoveryCfg(
            feeds=list(d.get("feeds", [])),
            keywords=[k.lower() for k in d.get("keywords", [])],
            max_age_days=int(d.get("max_age_days", 14)),
        ),
        verification=VerificationCfg(
            official_domains=[x.lower() for x in v.get("official_domains", [])],
            reputable_domains=[x.lower() for x in v.get("reputable_domains", [])],
        ),
        llm=LlmCfg(**_section(raw, "llm")),
        tts=TtsCfg(**_section(raw, "tts")),
        render=RenderCfg(**_section(raw, "render")),
        higgsfield=HiggsfieldCfg(**_section(raw, "higgsfield")),
        zernio=ZernioCfg(**_section(raw, "zernio")),
        costs=CostsCfg(**_section(raw, "costs")),
        gates=GatesCfg(**_section(raw, "gates")),
        trends=TrendsCfg(**_section(raw, "trends")),
        source=source,
    )


def load(path: Path | None = None) -> Config:
    candidates = (path,) if path else DEFAULT_CONFIG_PATHS
    for candidate in candidates:
        if candidate and candidate.exists():
            with candidate.open("rb") as fh:
                return from_dict(tomllib.load(fh), source=candidate)
    raise FileNotFoundError(f"no config found (tried {', '.join(map(str, candidates))})")


def secret(name: str) -> str | None:
    """Read a secret from the environment. Returns None when unset or empty."""
    value = os.environ.get(name, "").strip()
    return value or None
