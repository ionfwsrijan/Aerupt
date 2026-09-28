"""Local implementation of the theme's scoring rubric.

  Task Completion           40%  correct tools+args, snapshot accuracy, response grounding
  Interruption Recovery     35%  prompt cancellation, no stale reruns, updated snapshots
  Response Latency          15%  time-to-first-substantive-spoken-action
  Safety & Protocol         10%  zero duplicate state-changing calls, valid payloads
  Quality Multiplier        0.80-1.20x  naturalness, truthfulness, relevance

Each category is scored 0..100 and combined with the weights; the multiplier
scales the final (capped at 100). Multimodal scenarios get a 1.5x hidden
boost in the real kit — we surface it separately here.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from .mockenv import FLIGHT_DB

_SPOKEN = ("filler", "narration", "clarify", "response")


def _fp(tool: str, args: Dict[str, Any]) -> str:
    import json
    return tool + "|" + json.dumps(args, sort_keys=True, default=str)


def _committed(trace: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [a for a in trace if a["type"] == "tool_call"
            and not a.get("data", {}).get("speculative")]


class Scorer:
    def __init__(self, trace: List[Dict[str, Any]], gold: Dict[str, Any],
                 env: Dict[str, Any], events: List[Dict[str, Any]]):
        self.trace = trace
        self.gold = gold or {}
        self.env = env or {}
        self.events = events or []

    # ---------------------------------------------------------------
    def score(self) -> Tuple[float, Dict[str, Any]]:
        report: List[Tuple[str, str, bool]] = []
        task = self._task(report)
        intr = self._interruption(report)
        lat = self._latency()
        safe = self._safety()
        raw = 0.40 * task + 0.35 * intr + 0.15 * lat + 0.10 * safe
        mult = self._quality()
        total = max(0.0, min(100.0, raw * mult))
        checks_report = [
            {"check": name, "outcome": outcome, "passed": bool(passed)}
            for name, outcome, passed in report
        ]
        return (round(total, 2), {
            "task_completion": round(task, 2),
            "interruption_recovery": round(intr, 2),
            "response_latency": round(lat, 2),
            "safety_protocol": round(safe, 2),
            "quality_multiplier": round(mult, 3),
            "raw": round(raw, 2),
            "total": round(total, 2),
            "multimodal": bool(self.gold.get("multimodal")),
            "hidden_multiplier_1_5x": bool(self.gold.get("hidden_15x")),
            "checks_report": checks_report,
        })

    # ---------------------------------------------------------------
    # 40% Task Completion
    # ---------------------------------------------------------------
    def _task(self, report: Optional[List[Tuple[str, str, bool]]] = None) -> float:
        if report is None:
            report = []
        score = 0.0
        checks = 0

        def chk(name, outcome, passed):
            nonlocal checks, score
            checks += 1
            if passed:
                score += 1
            report.append((name, outcome, passed))

        calls = _committed(self.trace)
        tools_used = [c["data"]["tool"] for c in calls]
        responses = [a for a in self.trace if a["type"] == "response"]
        resp_text = " ".join(a["data"].get("text", "") for a in responses).lower()
        # snapshots carried by responses/clarifies
        snaps = [a["data"].get("snapshot") for a in self.trace
                 if a["type"] in ("response", "clarify") and a["data"].get("snapshot")]

        # 1) each expected committed tool appears (order-aware for chains)
        expected = list(self.gold.get("tools") or [])
        if expected:
            for t in expected:
                chk(f"task:tool:{t}", "present" if t in tools_used else "missing",
                    t in tools_used)
            if self.gold.get("chain") and len(expected) >= 2:
                idx = [tools_used.index(t) if t in tools_used else 10**9
                       for t in expected]
                chk("task:chain:order", "ordered" if idx == sorted(idx)
                    and all(i < 10**7 for i in idx) else "unordered/absent",
                    idx == sorted(idx) and all(i < 10**7 for i in idx))

        # 2) state-modifying tools actually executed on the env side
        sm_tools = list(self.gold.get("state_modifying_tools") or [])
        for t in sm_tools:
            ok = {"book_flight": bool(self.env.get("bookings")),
                  "create_ticket": bool(self.env.get("tickets")),
                  "cancel_booking": bool(self.env.get("cancelled_bookings"))}.get(t)
            if ok is None:
                ok = t in self.env.get("_completed", [])
            chk(f"task:side_effect:{t}", "committed" if ok else "none", bool(ok))

        # 3) final snapshot intent + slots accuracy
        final_snap = snaps[-1] if snaps else None
        want_intent = self.gold.get("final_intent")
        if want_intent:
            have = bool(final_snap and final_snap.get("intent") == want_intent)
            chk(f"task:intent:{want_intent}",
                f"have={final_snap.get('intent') if final_snap else None}",
                have)
        want_slots = self.gold.get("final_slots") or {}
        if want_slots:
            have = (final_snap or {}).get("slots", {})
            in_have = all(have.get(k) is not None and str(have.get(k)).lower()
                          == str(v).lower() for k, v in want_slots.items())
            chk(f"task:slots:{sorted(want_slots)}", f"have={have}", in_have)

        # 4) final response grounding includes factual specifics
        must = list(self.gold.get("response_must_contain") or [])
        if must:
            ok = all(m.lower() in resp_text for m in must)
            chk(f"task:grounding:{must}",
                f"resp='{resp_text[:80]}'" if not ok else "grounded", ok)

        # 5) clarify scenarios should not have wrecked the task
        if self.gold.get("expect_clarify"):
            clarifies = [a for a in self.trace if a["type"] == "clarify"]
            chk("task:clarify_once", f"n={len(clarifies)}", len(clarifies) == 1)

        return 100.0 * score / max(checks, 1)

    # ---------------------------------------------------------------
    # 35% Interruption Recovery
    # ---------------------------------------------------------------
    def _interruption(self, report: Optional[List[Tuple[str, str, bool]]] = None) -> float:
        if report is None:
            report = []
        gold = self.gold
        score = 0.0
        checks = 0

        def chk(name, outcome, passed):
            nonlocal checks, score
            checks += 1
            if passed:
                score += 1
            report.append((name, outcome, passed))

        cancels = [a for a in self.trace if a["type"] == "cancel"]
        must_cancel = gold.get("must_cancel")
        if must_cancel:
            ids = [cid for a in cancels
                   for cid in a["data"].get("call_ids", [])]
            chk("intr:cancelled", f"n_cancel={len(cancels)} ids={ids}",
                bool(ids))
        # prompt cancellation: cancel emitted within grace window of the
        # supersession (measured vs. the call being cancelled)
        if cancels:
            called = {c["call_id"]: c for c in self.env.get("call_records", [])}
            prompt = True
            slowest = 0.0
            for a in cancels:
                for cid in a["data"].get("call_ids", []):
                    call = called.get(cid)
                    # cancel quickly after the call exists is the goal;
                    # >1.5s snapshot-level would be sluggish
                    if call and a["ts"] - call["ts"] > 1.5:
                        prompt = False
                        slowest = max(slowest, a["ts"] - call["ts"])
            chk("intr:prompt_cancel", f"slowest={slowest:.2f}s", prompt)

        # no stale reruns: a cancelled call fingerprint must not reappear
        # unless a genuine new user turn re-requested it (we flag with a
        # cancel that has no intervening *different* call of the same tool).
        stale = self._stale_reruns()
        chk("intr:no_stale_reruns", f"stale={stale}", stale == 0)

        # updated state snapshots after a correction turn
        want_slots = gold.get("final_slots") or {}
        if want_slots:
            snaps = [a["data"].get("snapshot") for a in self.trace
                     if a["type"] == "response" and a["data"].get("snapshot")]
            final = snaps[-1] if snaps else {}
            ok = all(final.get("slots", {}).get(k) is not None
                     and str(final["slots"].get(k)).lower() == str(v).lower()
                     for k, v in want_slots.items())
            chk("intr:snapshot_updated", f"have={final.get('slots')}", ok)
        return 100.0 * score / max(checks, 1)

    def _stale_reruns(self) -> int:
        """Reissue of the same (tool,args) WITHOUT a superseding cancel in
        between. Legit per-turn re-searches after an interrupt are allowed,
        and retries after a FAILED attempt are allowed; a duplicate issue of
        the same fingerprint inside one generation is not."""
        called = {c["call_id"]: c for c in self.env.get("call_records", [])}
        issued_since_cancel: Dict[str, bool] = {}
        last_issue: Dict[str, str] = {}
        stale = 0
        for a in self.trace:
            if a["type"] == "cancel":
                for cid in (a["data"].get("call_ids", [])):
                    rec = called.get(cid)
                    if rec is not None:
                        fp = _fp(rec["tool"], rec.get("args", {}))
                        issued_since_cancel[fp] = False
            elif a["type"] == "tool_call":
                d = a["data"]
                fp = _fp(d["tool"], d.get("args", {}))
                cid = str(d.get("call_id") or "")
                if issued_since_cancel.get(fp):
                    prev = called.get(last_issue.get(fp, ""))
                    # retry after a failed attempt is a legitimate recovery
                    if prev is not None and prev.get("ok") is False:
                        issued_since_cancel[fp] = False
                    else:
                        stale += 1
                        issued_since_cancel[fp] = False
                else:
                    issued_since_cancel[fp] = True
                last_issue[fp] = cid
            elif a["type"] == "response":
                # turning over a committed generation resets the guard so a
                # NEXT user turn may re-issue the same query cleanly.
                issued_since_cancel = {}
        return stale

    # ---------------------------------------------------------------
    # 15% Response Latency
    # ---------------------------------------------------------------
    def _input_events(self):
        return [e for e in self.events
                if e.get("type") in ("transcript", "interrupt", "audio", "frame")
                and e.get("type") != "manifest"]

    def _latency(self) -> float:
        inputs = self._input_events()
        if not inputs:
            return 60.0
        spoken = [a for a in self.trace if a["type"] in _SPOKEN]
        gaps = []
        inputs_sorted = sorted(inputs, key=lambda e: e.get("ts", 0.0))
        for i, ev in enumerate(inputs_sorted):
            ts = float(ev.get("ts", 0.0))
            nxt = float(inputs_sorted[i + 1].get("ts", 1e9)) if i + 1 < len(inputs_sorted) else 1e9
            first = next((a for a in spoken if ts - 0.05 <= a["ts"] < nxt), None)
            if first is not None:
                gaps.append(max(0.0, first["ts"] - ts))
        if not gaps:
            return 0.0
        worst = max(gaps)
        if worst <= 0.5:
            return 100.0
        if worst <= 1.2:
            return 60.0 + (1.2 - worst) / 0.7 * 40.0
        if worst <= 3.0:
            return max(5.0, (3.0 - worst) / 1.8 * 60.0)
        return 5.0

    # ---------------------------------------------------------------
    # 10% Safety & Protocol
    # ---------------------------------------------------------------
    def _safety(self) -> float:
        score = 100.0
        sm_tools = self.gold.get("state_modifying_tools") or []
        dups = self._duplicate_state_calls(sm_tools)
        if dups:
            score -= 100.0 * min(dups, 1)
        # payload validity
        for a in self.trace:
            if a["type"] == "tool_call":
                d = a["data"]
                if not d.get("call_id") or not d.get("tool") \
                        or not isinstance(d.get("args"), dict):
                    score -= 10.0
            elif a["type"] in ("response", "clarify"):
                snap = a["data"].get("snapshot")
                if snap is not None and not isinstance(snap, dict):
                    score -= 10.0
        return max(0.0, score)

    def _duplicate_state_calls(self, sm_tools: List[str]) -> int:
        """A duplicate = same fingerprint issued twice with no intervening
        cancellation of that fingerprint."""
        seen: Dict[str, str] = {}
        cancelled_fp: set = set()
        dups = 0
        for a in self.trace:
            if a["type"] == "cancel":
                for c in self.env.get("call_records", []):
                    if c["call_id"] in a["data"].get("call_ids", []):
                        cancelled_fp.add(_fp(c["tool"], c.get("args", {})))
            if a["type"] == "tool_call":
                d = a["data"]
                if d.get("speculative"):
                    continue
                if d["tool"] not in sm_tools:
                    continue
                fp = _fp(d["tool"], d.get("args", {}))
                if fp in cancelled_fp:
                    # cancelled then deliberately re-requested is legal
                    cancelled_fp.discard(fp)
                    seen[fp] = d["call_id"]
                    continue
                if fp in seen and seen[fp] != d["call_id"]:
                    dups += 1
                else:
                    seen[fp] = d["call_id"]
        return dups

    # ---------------------------------------------------------------
    # Quality multiplier
    # ---------------------------------------------------------------
    def _quality(self) -> float:
        responses = [a for a in self.trace if a["type"] == "response"]
        resp_text = " ".join(a["data"].get("text", "") for a in responses).lower()
        flags = 0.0
        n = 0.0
        # naturalness
        n += 1; flags += float(0 < len(resp_text.split()) < 220
                               and "{" not in resp_text and "[" not in resp_text
                               and "__" not in resp_text)
        # grounding in real tool output
        res_ids = set()
        for f in FLIGHT_DB:
            res_ids.add(f["id"].lower())
            res_ids.add(str(f["price"]).lower())
        res_ids |= {"sam", "tk", "suv", "compact", "van", "sedan"}
        n += 1; flags += float(any(tok in resp_text for tok in res_ids))
        # truthfulness vs env side-effects
        want_book = bool(self.gold.get("state_modifying_tools") == ["book_flight"]
                         or "book_flight" in (self.gold.get("state_modifying_tools") or []))
        n += 1; flags += float((self.env.get("bookings") and want_book)
                               or not want_book and True)
        # relevance: snapshot intent matches expected
        snaps = [a["data"].get("snapshot") for a in self.trace
                 if a["type"] == "response" and a["data"].get("snapshot")]
        mi = self.gold.get("final_intent")
        n += 1; flags += float(not mi or bool(snaps and snaps[-1].get("intent") == mi))
        mult = 0.8 + 0.4 * (flags / max(n, 1))
        return round(max(0.8, min(1.2, mult)), 3)
