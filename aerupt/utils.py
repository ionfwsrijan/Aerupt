"""Shared utilities: injectable clock, throttles, canonicalization, hashing."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time as _time
from typing import Any, Dict, Optional

MS = 0.001


class Clock:
    """Injectable time source.

    Defaults to real monotonic wall-time. The evaluation harness uses a
    Virtual Clock; pass a clock with the same `now()`/`sleep()` surface to run
    the agent deterministically without waiting real wall-time.
    """

    def __init__(self, scale: float = 1.0, epoch: float = 0.0,
                 now: Optional[Any] = None):
        self._scale = max(scale, 0.0)
        self._base = (now() if now else _time.monotonic()) - epoch

    def now(self) -> float:
        return _time.monotonic() - self._base

    async def sleep(self, dt: float) -> None:
        if self._scale <= 0 or dt <= 0:
            return
        await asyncio.sleep(dt * self._scale)

    def real_sleep(self, dt: float) -> None:
        _time.sleep(max(dt * self._scale, 0.0))


class Throttle:
    """Emit-at-most-once-per-window guard (used for filler spam control)."""

    def __init__(self, window_ms: float = 0.0):
        self._window = window_ms * MS
        self._last: Dict[str, float] = {}

    def allow(self, clock: Clock, key: str = "default") -> bool:
        now = clock.now()
        if now - self._last.get(key, -1e9) >= self._window:
            self._last[key] = now
            return True
        return False


def now_ms(clock: Clock) -> float:
    return clock.now() * 1000.0


def canonical_json(obj: Any) -> str:
    """Deterministic JSON for fingerprints — key order is ignored."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def fingerprint(*parts: Any) -> str:
    return hashlib.sha1(canonical_json(parts).encode("utf-8")).hexdigest()[:16]


def norm(text: str) -> str:
    """Lowercase, strip, collapse whitespace for matching."""
    out = re.sub(r"\s+", " ", text or "").strip().lower()
    return re.sub(r"[“”\"'`(),.;:!?]+", "", out)


def norm_keep_apos(text: str) -> str:
    out = re.sub(r"\s+", " ", text or "").strip().lower()
    return re.sub(r"[“”\"`,().;:!?]+", "", out)


def title_case(phrase: str) -> str:
    return " ".join(w[:1].upper() + w[1:] for w in re.split(r"\s+", phrase.strip()) if w)


def safe_int(x: Any, default: Optional[int] = None) -> Optional[int]:
    try:
        return int(x)
    except (TypeError, ValueError):
        return default


def deep_merge(base: Dict[str, Any], extra: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out