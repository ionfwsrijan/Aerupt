"""Session-scoped State Snapshot.

Mirrors the "Session Slot Tracking" objective:
  - keeps intent + slot values across multi-turn interactions
  - applies *localized* corrections: a turn only overrides slots it
    explicitly mentions, so "no, to London" updates destination only
  - versioned for trace/scorer introspection

Snapshot format (returned inside `response` actions):
  {
    "intent": "flight_search",
    "slots": {"origin": "paris", "destination": "london"},
    "version": 2,
    "turn": 1,
    "uncertain": ["date"]          # slots required but not yet grounded
  }
"""
from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional

from .utils import deep_merge


class StateSnapshot:
    def __init__(self, scenario_today: Optional[date] = None):
        self.intent: Optional[str] = None
        self.slots: Dict[str, Any] = {}
        self.meta: Dict[str, Any] = {}
        self.version: int = 0
        self.turn_count: int = 0
        self.updated_at: float = 0.0
        self.uncertain: List[str] = []
        self.reference_date: date = scenario_today or date.today()

    # ------------------------------------------------------------------
    def apply_turn(self, intent: Optional[str], turn_slots: Dict[str, Any],
                   turn_n: int, ts: float, corrected: bool = False) -> bool:
        """Merge a parsed turn into the snapshot using *localized override*
        semantics, then bump the version. Returns True if anything changed."""
        before = (self.intent, self.slots, self.version)
        if intent:
            self.intent = intent
        merged = deep_merge(self.slots, turn_slots or {})
        if merged != self.slots:
            self.slots = merged
        self.meta["last_correction"] = bool(corrected)
        self.turn_count = turn_n
        self.updated_at = ts
        changed = (self.intent, self.slots) != before[:2]
        if changed:
            self.version += 1
        return changed

    def set_result(self, key: str, value: Any) -> None:
        """Ground a slot from an authoritative source (tool result)."""
        self.slots[key] = value
        self.version += 1

    def set_meta(self, key: str, value: Any) -> None:
        self.meta[key] = value

    def mark_uncertain(self, keys: List[str]) -> None:
        self.uncertain = list(keys)

    def get(self, key: str, default: Any = None) -> Any:
        return self.slots.get(key, default)

    # ------------------------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        return {
            "intent": self.intent,
            "slots": dict(self.slots),
            "version": self.version,
            "turn": self.turn_count,
            "uncertain": list(self.uncertain),
            "reference_date": self.reference_date.isoformat(),
        }

    def to_full_dict(self) -> Dict[str, Any]:
        out = self.to_dict()
        out["meta"] = dict(self.meta)
        return out