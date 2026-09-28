"""Tool registry + idempotency.

The agent never executes tools itself — it *requests* them through
`tool_call` actions and receives results asynchronously via `tool_result`
events (the harness / mock env owns execution). This module handles:

  - parsing dynamic tool manifests into `ToolSpec`s (read-only vs. state-
    modifying, JSON-schema-ish params)
  - generating unique `call_id`s
  - the idempotency ledger: guarantees zero duplicate state-changing calls
    (fingerprint on canonical tool+args); completed calls are remembered
    for the session so a correction mid-booking can't double-book.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from .utils import fingerprint

STATE_MOD_KEYS = ("state_modifying", "state_changing", "writes", "write",
                  "mutating", "side_effect", "non_readonly", "destructive")
READONLY_KEYS = ("read_only", "readonly", "safe", "query")

_CALL_COUNTER = 0


class ToolSpec:
    def __init__(self, name: str, spec: Optional[Dict[str, Any]] = None):
        spec = spec or {}
        self.name = name
        self.params: List[Dict[str, Any]] = []
        raw_params = spec.get("params") or spec.get("parameters") or []
        for p in raw_params:
            if isinstance(p, str):
                self.params.append({"name": p})
            elif isinstance(p, dict):
                d = dict(p)
                d.setdefault("name", "")
                self.params.append({k: v for k, v in d.items()})
        self.state_modifying = self._infer_state_modifying(spec)
        self.optional: List[str] = [p["name"] for p in self.params
                                    if not p.get("required")
                                    and p.get("optional") is not False
                                    and p.get("default") is not None]
        self.required: List[str] = [p["name"] for p in self.params
                                    if p.get("required")]
        self.description = spec.get("description") or spec.get("desc") or ""

    @staticmethod
    def _infer_state_modifying(spec: Dict[str, Any]) -> bool:
        for k in STATE_MOD_KEYS:
            if k in spec and bool(spec[k]):
                return True
        for k in READONLY_KEYS:
            if k in spec and spec[k] is True:
                return False
        # param-name heuristic: id/query/limit/search/filter/enum-only -> read
        name = spec.get("name", "").lower()
        if any(t in name for t in ("search", "lookup", "find", "get", "list", "query")):
            return False
        if any(t in name for t in ("book", "create", "log", "file", "order",
                                   "buy", "reserve", "insert", "update", "delete", "cancel")):
            return True
        return False

    def enum_for(self, param: str) -> List[Any]:
        for p in self.params:
            if p.get("name") == param:
                return list(p.get("enum") or p.get("options") or [])
        return []

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "params": self.params,
                "state_modifying": self.state_modifying}

    def __repr__(self) -> str:  # pragma: no cover
        return (f"<ToolSpec {self.name} state_modifying={self.state_modifying}")


class ToolRegistry:
    def __init__(self):
        self.tools: Dict[str, ToolSpec] = {}
        self._seq = 0

    def load_manifest(self, payload: Any) -> List[str]:
        """Parse a scenario manifest. Returns newly learned tool names."""
        tools = self._coerce_tools(payload)
        added = []
        for item in tools:
            name = self._coerce_name(item)
            if not name:
                continue
            self.tools[name] = ToolSpec(name, item)
            added.append(name)
        return added

    def _coerce_tools(self, payload: Any) -> List[Dict[str, Any]]:
        if isinstance(payload, list):
            return [t for t in payload if isinstance(t, dict)]
        if isinstance(payload, dict):
            raw = payload.get("tools") or payload.get("tool_specs") or payload.get("definitions")
            if isinstance(raw, list):
                return [t for t in raw if isinstance(t, dict)]
            vals = list(payload.values())
            if vals and all(isinstance(v, dict) for v in vals):
                return vals
        return []

    def _coerce_name(self, item: Dict[str, Any]) -> str:
        name = str(item.get("name") or item.get("tool") or item.get("id") or "")
        return name.replace("__", "_").replace("-", "_").lower()

    def get(self, name: str) -> Optional[ToolSpec]:
        return self.tools.get(name)

    def is_state_modifying(self, name: str) -> bool:
        spec = self.tools.get(name)
        return spec.state_modifying if spec else False

    def known_names(self) -> List[str]:
        return list(self.tools.keys())

    def next_call_id(self, tool: str) -> str:
        self._seq += 1
        return f"c{self._seq:04d}"


class IdempotencyLedger:
    """Fingerprint-level dedupe for state-changing calls.

    - `issued`   : request fingerprint -> call bucket
    - `done`     : request fingerprint -> confirmed completed result
    - `uncertain`: request fingerprint -> result never arrived (need care)
    """

    def __init__(self, window_s: float = 3600.0):
        self.window_s = window_s
        self.issued: Dict[str, Dict[str, Any]] = {}
        self.done: Dict[str, Any] = {}
        self.created_at = time.time()

    def mark_issued(self, tool: str, args: Dict[str, Any], call_id: str,
                    fingerprint_: Optional[str] = None) -> str:
        fp = fingerprint_ or fingerprint(tool, args)
        bucket = self.issued.setdefault(
            fp, {"tool": tool, "args": args, "call_ids": [], "cancelled": False})
        bucket["call_ids"].append(call_id)
        return fp

    def mark_completed(self, tool: str, args: Dict[str, Any], result: Any,
                       fingerprint_: Optional[str] = None) -> str:
        fp = fingerprint_ or fingerprint(tool, args)
        self.done[fp] = {"tool": tool, "args": args, "result": result,
                         "ts": time.time()}
        return fp

    def mark_cancelled(self, tool: str, args: Dict[str, Any],
                       fingerprint_: Optional[str] = None) -> str:
        fp = fingerprint_ or fingerprint(tool, args)
        bucket = self.issued.get(fp)
        if bucket:
            bucket["cancelled"] = True
        return fp

    def mark_failed(self, tool: str, args: Dict[str, Any],
                    fingerprint_: Optional[str] = None) -> str:
        fp = fingerprint_ or fingerprint(tool, args)
        bucket = self.issued.get(fp)
        if bucket:
            bucket["failed"] = True
        return fp

    def resolve_issue(self, tool: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """Decision helper for issuing a state-modifying call.

        Returns one of:
          {'kind': 'done',    'result': r}   reuse completed result (no dup)
          {'kind': 'inflight','call_ids': [...]} already issued, no re-issue
          {'kind': 'new',     'result': None} safe to issue
        """
        fp = fingerprint(tool, args)
        done = self.done.get(fp)
        if done:
            return {"kind": "done", "result": done["result"], "fp": fp}
        bucket = self.issued.get(fp)
        if bucket and not bucket.get("cancelled") and not bucket.get("failed"):
            return {"kind": "inflight", "call_ids": list(bucket["call_ids"]), "fp": fp}
        return {"kind": "new", "result": None, "fp": fp}

    def already_done(self, tool: str, args: Dict[str, Any]) -> Any:
        return self.done.get(fingerprint(tool, args))

    def in_flight(self, tool: str, args: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        return self.issued.get(fingerprint(tool, args))

    def supersede(self, tool: str, args: Dict[str, Any]) -> str:
        fp = fingerprint(tool, args)
        self.mark_cancelled(tool, args, fp)
        return fp

    def done_fingerprints(self) -> List[str]:
        return list(self.done.keys())