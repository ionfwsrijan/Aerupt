"""Agent configuration knobs. All timings in milliseconds unless noted."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict


def load_env() -> None:
    """Best-effort `.env` loader. Never fails if python-dotenv is absent."""
    try:
        from dotenv import load_dotenv
        loaded = load_dotenv()
        if loaded:
            os.environ.setdefault("AERUPT_ENV", "dotenv")
    except Exception:
        pass


@dataclass
class Config:
    # Fast-path timing
    ack_delay_ms: float = 60.0            # after end-of-turn, min delay before first spoken action
    intro_narrate_ms: float = 150.0       # progress narration fires shortly after turn commit
    progress_filler_ms: float = 1400.0    # if slow path silent this long, emit progress narration
    long_task_filler_ms: float = 3500.0   # second-stage progress narration
    speculation_min_ms: float = 90.0      # don't speculate faster than this after a chunk
    interrupt_ack_ms: float = 40.0        # ack an interruption this fast

    # Slow-path / coordination
    cancel_grace_ms: float = 30.0         # grace period before hard-cancelling in-flight calls
    max_retries: int = 2                  # read-only tool retries on provable failure
    retry_backoff_ms: float = 120.0
    clarify_limit: int = 1                # ask for a single slot at a time
    chain_auto_select: bool = True        # auto-pick best flight on chained book when unambiguous
    relax_empty_search: bool = True       # re-run empty searches without the date constraint

    # Safety
    idempotency_window_s: float = 3600.0  # how long completed state-changing calls are remembered

    # Behavior
    speculation_enabled: bool = True
    require_complete_args_to_speculate: bool = False  # if False, speculate on partial-but-satisfying args
    quality_verbose: bool = True          # include facts in narration (substantive fast path)

    # Environment
    time_scale: float = 1.0
    extra: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Config":
        known = {f for f in cls.__dataclass_fields__ if f != "extra"}
        conf = cls()
        for k, v in (d or {}).items():
            if k in known:
                setattr(conf, k, v)
            else:
                conf.extra[k] = v
        return conf