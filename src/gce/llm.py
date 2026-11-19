"""Thin LLM wrapper that returns text plus a cost estimate."""

from __future__ import annotations

from dataclasses import dataclass

from gce.config import LlmCfg, secret


@dataclass(frozen=True)
class LlmResult:
    text: str
    input_tokens: int
    output_tokens: int
    usd: float


def cost(cfg: LlmCfg, input_tokens: int, output_tokens: int) -> float:
    return (
        input_tokens * cfg.input_usd_per_mtok + output_tokens * cfg.output_usd_per_mtok
    ) / 1_000_000


def complete(cfg: LlmCfg, system: str, prompt: str) -> LlmResult:
    if cfg.provider != "anthropic":
        raise ValueError(f"unsupported llm provider: {cfg.provider}")
    api_key = secret("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. Either set it, or write the script yourself and import"
            " it with `gce script import <file.json>` (no API cost)."
        )
    import anthropic

    client = anthropic.Anthropic(api_key=api_key)
    msg = client.messages.create(
        model=cfg.model,
        max_tokens=cfg.max_tokens,
        system=system,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(block.text for block in msg.content if block.type == "text")
    usage = msg.usage
    return LlmResult(
        text=text,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        usd=cost(cfg, usage.input_tokens, usage.output_tokens),
    )
