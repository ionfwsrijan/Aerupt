"""LLM integration (Groq, OpenAI-compatible endpoint) with offline fallback.

The agent is fully functional without any LLM key: every generation site has a
deterministic fallback. When `GROQ_API_KEY` is set (and `AERUPT_LLM != "0"`), the
Groq chat-completions API is used to compose grounded answers over RAG context.

Configuration (environment):
  GROQ_API_KEY        Groq api key (or LLM_API_KEY)
  GROQ_MODEL          model id, default "llama-3.3-70b-versatile"
  LLM_BASE_URL        override endpoint (default https://api.groq.com/openai/v1)
  LLM_TIMEOUT_S       per-call timeout (default 8)
  AERUPT_LLM            set to "0" to disable the LLM path explicitly
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Optional

GROQ_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_MODEL = "llama-3.3-70b-versatile"


@dataclass(frozen=True)
class LLMConfig:
    api_key: Optional[str]
    model: str
    base_url: str
    timeout_s: float
    enabled: bool

    @property
    def human_label(self) -> str:
        return f"groq:{self.model}" if self.enabled else "deterministic"


def load_llm_config() -> LLMConfig:
    key = os.environ.get("GROQ_API_KEY") or os.environ.get("LLM_API_KEY") or None
    enabled = bool(key)
    if os.environ.get("AERUPT_LLM", "").strip() == "0":
        enabled = False
    return LLMConfig(
        api_key=key if enabled else None,
        model=os.environ.get("GROQ_MODEL", DEFAULT_MODEL),
        base_url=os.environ.get("LLM_BASE_URL", GROQ_BASE_URL),
        timeout_s=float(os.environ.get("LLM_TIMEOUT_S", "8")),
        enabled=enabled,
    )


def llm_available() -> bool:
    return load_llm_config().enabled


def llm_model() -> str:
    return load_llm_config().model


async def complete(
    system: str,
    user: str,
    *,
    temperature: float = 0.3,
    max_tokens: int = 700,
) -> Optional[str]:
    """One chat-completion round-trip. Returns None when disabled or on any
    failure so callers always fall back to deterministic generation."""
    cfg = load_llm_config()
    if not cfg.enabled or not cfg.api_key:
        return None
    try:
        from groq import AsyncGroq
    except Exception:
        return None
    client = AsyncGroq(api_key=cfg.api_key, base_url=cfg.base_url,
                       timeout=cfg.timeout_s)
    try:
        resp = await asyncio.wait_for(
            client.chat.completions.create(
                model=cfg.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=temperature,
                max_tokens=max_tokens,
            ),
            timeout=cfg.timeout_s,
        )
        text = (resp.choices[0].message.content or "").strip()
        return text or None
    except Exception:
        return None