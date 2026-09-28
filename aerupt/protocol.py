"""Protocol types shared between the agent and any harness.

The agent communicates over two asynchronous queues of timestamped dicts:

    input  : {"ts": float, "type": str, "data": dict}
    output : {"ts": float, "type": str, "data": dict}

Input event types (normalized):
    manifest, transcript, start_of_turn, end_of_turn, audio, frame,
    interrupt, tool_result, end

Output action types:
    filler, narration, tool_call, cancel, clarify, response, snapshot
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

EVENT_TRANSCRIPT = "transcript"
EVENT_MANIFEST = "manifest"
EVENT_AUDIO = "audio"
EVENT_FRAME = "frame"
EVENT_INTERRUPT = "interrupt"
EVENT_TOOL_RESULT = "tool_result"
EVENT_START_OF_TURN = "start_of_turn"
EVENT_END = "end"

ACTION_FILLER = "filler"
ACTION_NARRATION = "narration"
ACTION_TOOL_CALL = "tool_call"
ACTION_CANCEL = "cancel"
ACTION_CLARIFY = "clarify"
ACTION_RESPONSE = "response"
ACTION_SNAPSHOT = "snapshot"

RESERVED_TYPES = {
    EVENT_TRANSCRIPT, EVENT_MANIFEST, EVENT_AUDIO, EVENT_FRAME,
    EVENT_INTERRUPT, EVENT_TOOL_RESULT, EVENT_START_OF_TURN, EVENT_END,
}


@dataclass
class Event:
    type: str
    data: Dict[str, Any] = field(default_factory=dict)
    ts: float = 0.0

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Event":
        d = dict(d or {})
        t = str(d.get("type") or "")
        raw = d.get("data")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception:
                raw = {"raw": raw}
        if not isinstance(raw, dict):
            raw = {"raw": raw}
        try:
            ts = float(d.get("ts", 0.0))
        except (TypeError, ValueError):
            ts = 0.0
        return cls(type=t, data=raw, ts=ts)

    def is_turn_boundary(self) -> bool:
        return self.type in (EVENT_END_OF_TURN_FLAG,)


@dataclass
class Action:
    type: str
    data: Dict[str, Any] = field(default_factory=dict)
    ts: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {"ts": round(self.ts, 4), "type": self.type, "data": self.data}


def actions_to_jsonl(actions: List[Action]) -> str:
    return "\n".join(json.dumps(a.to_dict()) for a in actions)


# Transcript chunks carry an explicit end-of-turn marker flag inside data.
EVENT_END_OF_TURN_FLAG = "end_of_turn"  # data key