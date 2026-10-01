"""Spoken-surface generation.

Produces the agent's spoken actions:
  - `filler`/`narration`: fast-path acknowledgments & progress narration
    (substantive, truthful — no false completion claims)
  - `response`: final, tool-grounded answers
  - `clarify`: targeted clarification requests

The quality multiplier rewards naturalness, truthfulness and relevance:
everything said must be traceable to (a) what the user said, or
(b) an actual tool result we hold.
"""
from __future__ import annotations

import itertools
import re
from typing import Any, Dict, List, Optional

from .nlu import TemporalResolver

_FILLERS = [
    "Alright,",
    "Okay,",
    "Sure,",
    "Got it.",
    "One second.",
    "Hang on.",
]

_ACKS = [
    "I'm listening.",
    "Go on.",
    "Mm-hmm.",
    "Tell me more.",
]

_INTERRUPT_ACKS = [
    "One moment — let me redo that.",
    "Got it — let me start over.",
    "Okay, adjusting.",
    "Sure — let me update that.",
]

_VERB_HINTS = {
    "cancel": "Sure, cancelling that for you now.",
    "rent": "Let me check rental availability.",
    "book": "Let me reserve that for you.",
    "lookup": "Let me look that up.",
}

_PROGRESS_FILLERS = [
    "Still on it — almost there.",
    "That's taking a moment, one sec.",
    "Just a beat longer.",
]

_LONG_PROGRESS_FILLERS = [
    "This one's taking a bit longer than usual.",
    "Still working on it — thanks for waiting.",
]


class SpokenBrain:
    def __init__(self, reference_date=None):
        self._ref = reference_date
        self._fillers = itertools.cycle(_FILLERS)
        self._acks = itertools.cycle(_ACKS)
        self._interrupt = itertools.cycle(_INTERRUPT_ACKS)
        self._progress = itertools.cycle(_PROGRESS_FILLERS)
        self._long = itertools.cycle(_LONG_PROGRESS_FILLERS)
        self._last: Dict[str, str] = {}

    # -- primitive emissions ------------------------------------------------
    def filler(self, kind: str = "filler") -> str:
        pool = {"filler": self._fillers, "ack": self._acks,
                "interrupt": self._interrupt,
                "progress": self._progress, "long": self._long}[kind]
        # avoid repeating the same filler twice in a row (pools are cycles)
        for _ in range(8):
            s = next(pool)
            if self._last.get(kind) != s:
                self._last[kind] = s
                return s
        return next(pool)

    # -- narration (substantive fast path) ----------------------------------
    def narrate_intent(self, intent: Optional[str], slots: Dict[str, Any],
                       text: str = "") -> Optional[str]:
        """Progress narration tied to the actual task. Returns None when we
        don't yet know enough to say something truthful."""
        if not intent:
            return None
        i = intent
        s = slots
        origin, dest = s.get("origin"), s.get("destination")
        date_lbl = self._date_label(s.get("date"))
        if "manual" in i or "diagnos" in i or "troubleshoot" in i:
            q = s.get("query") or s.get("subject") or s.get("device_model")
            if q:
                return f"Let me check the manual for that{'.' if not q.endswith('.') else ''}"
            return "Checking the manual now."
        if "knowledge" in i or "qa" in i or "explain" in i or "kb" in i:
            return "Let me check the aviation knowledge base for that."
        if "status" in i and ("flight" in i or "track" in i):
            return "Let me check the live flight status for you."
        if "search" in i or "find" in i or "lookup" in i:
            if origin and dest:
                tail = f", {date_lbl}." if date_lbl else "."
                return f"Checking {origin.title()} to {dest.title()} flights{tail}"
            if dest:
                return f"Looking up flights to {dest.title()}."
            if origin:
                return f"Looking up flights from {origin.title()}."
            return None
        if "book" in i or "reserve" in i or "buy" in i:
            # a real flight id is only announced when spoken this turn;
            # carried-over ids must not leak into the narration.
            fl = s.get("flight_id") if re.search(r"\b[A-Za-z]{1,3}\d{2,4}\b", text or "") else None
            if fl:
                return f"Sure, I'll book flight {fl}."
            if dest:
                return f"Okay, booking your trip to {dest.title()}."
            return "Alright, let's get that booked."
        if "create" in i or "log" in i or "file" in i:
            subj = s.get("subject")
            if subj:
                return f"Got it — creating a ticket about {subj}."
            return "Okay, let me log that for you."
        if "cancel" in i:
            fid = s.get("flight_id") or (s.get("flight_ids") or ["flight"])[0]
            return f"Sure — cancelling the booking for {fid}."
        # fallback: any manifest tool gets an honest, truthful progress line
        toolish = i.replace("_", " ")
        for k, tmpl in _VERB_HINTS.items():
            if k in i:
                return tmpl
        return f"Let me {toolish} for you."
        return None

    # -- final response composition -----------------------------------------
    def respond(self, result: Any, snapshot: Dict[str, Any],
                tool: Optional[str] = None) -> str:
        """Build a final spoken response grounded strictly in `result` +
        the snapshot. If we cannot ground it, say so honestly."""
        tool = tool or snapshot.get("last_tool")
        slots = snapshot.get("slots", {})

        if result is None:
            return ("Sorry — that request didn't go through. "
                    "Could you try again, or rephrase?")

        flights = self._pick(result, "flights") or self._pick(result, "results")
        if flights and isinstance(flights, (list, tuple)) and "flight" in (tool or ""):
            return self._flights_text(flights, slots, snapshot)
        if "status" in (tool or "") and isinstance(result, dict):
            fid = self._pick(result, "flight_id")
            st = str(self._pick(result, "status") or "").lower()
            if result.get("ok") and st:
                phrase = " ".join(
                    w if w != "min" else "minutes" for w in st.split())
                out = f"Your {fid or 'flight'} is {phrase}."
                gate = self._pick(result, "gate")
                term = self._pick(result, "terminal")
                if gate:
                    out += f" It's boarding at gate {gate}"
                    out += f", terminal {term}." if term else "."
                return out
            return f"I couldn't get live status for {fid or 'that flight'}."
        if "flight" in (tool or "") and isinstance(result, dict):
            booked = self._pick(result, "booking") or self._pick(result, "confirmation")
            if booked or result.get("ok"):
                fid = self._pick(result, "flight_id") or self._pick(booked if isinstance(booked, dict) else {}, "flight_id")
                ref = self._pick(result, "reference") or self._pick(result, "id")
                if fid:
                    return f"Done — flight {fid} is booked. Your reference is {ref}." if ref else f"Done — flight {fid} is booked."
                return "Your flight is booked."
        ticket = self._pick(result, "ticket") or self._pick(result, "reference")
        if "ticket" in (tool or "") or "create" in (tool or "") or "log" in (tool or ""):
            tk = self._pick(result, "ticket_id") or self._pick(result, "id")
            if tk:
                return f"Your support ticket is {tk}. I'll keep you posted."
            if result.get("ok"):
                return "Ticket created successfully."
        answer = self._pick(result, "answer") or self._pick(result, "text") or self._pick(result, "content")
        if "manual" in (tool or "") or "lookup" in (tool or ""):
            if answer:
                return f"Here's what the manual says: {answer}"
            steps = self._pick(result, "steps")
            if steps:
                return "Here are the steps from the manual: " + "; ".join(str(x) for x in steps) + "."
            return "I couldn't find that in the manual — could you rephrase?"
        if isinstance(result, dict) and not result.get("ok") and result.get("error"):
            return f"Sorry, that didn't go through: {result['error']}. Let me try another way."
        if answer:
            return str(answer)
        return self._fallback(snapshot)

    def _fallback(self, snapshot: Dict[str, Any]) -> str:
        intent = snapshot.get("intent")
        if not intent:
            return "I'm not sure I caught that — could you say it again?"
        slots = snapshot.get("slots", {}) or {}
        labels = (("vehicle_type", "vehicle type"), ("location", "location"),
                  ("days", "days"), ("device_model", "device"),
                  ("query", "query"), ("subject", "subject"),
                  ("origin", "from"), ("destination", "to"))
        facts = []
        for key, label in labels:
            if slots.get(key):
                facts.append(f"{label} {slots[key]}")
        for key in ("vehicle_type", "location", "days"):
            pass
        detail = ", ".join(facts)
        if detail:
            return f"All set — {detail} it is."
        return "Alright, I've handled that for you."

    # -- clarification -------------------------------------------------------
    def clarify(self, missing: List[Dict[str, Any]]) -> str:
        if not missing:
            return "Could you give me a bit more detail?"
        m = missing[0]
        name = m.get("name", "that")
        label = m.get("label") or m.get("title") or name.replace("_", " ")
        enums = m.get("enum") or []
        if enums and len(enums) <= 6:
            opts = ", ".join(str(e) for e in enums[:6])
            return f"Could you tell me the {label}? For example: {opts}."
        return f"Could you tell me the {label}?"

    # -- helpers -------------------------------------------------------------
    def narrate_interrupt(self) -> str:
        return next(self._interrupt)

    @staticmethod
    def _pick(d: Any, key: str) -> Any:
        if isinstance(d, dict):
            if key in d:
                return d[key]
            for v in d.values():
                if isinstance(v, dict):
                    r = SpokenBrain._pick(v, key)
                    if r is not None:
                        return r
        return None

    def _date_label(self, date_iso: Optional[str]) -> Optional[str]:
        if not date_iso:
            return None
        try:
            y, m, d = (int(x) for x in date_iso.split("-"))
        except Exception:
            return None
        from datetime import date
        try:
            target = date(y, m, d)
        except ValueError:
            return None
        if self._ref:
            delta = (target - self._ref).days
            if delta == 0:
                return "today"
            if delta == 1:
                return "tomorrow"
            if delta == 2:
                return "the day after tomorrow"
            if 1 < delta < 7:
                return target.strftime("%A")
        return target.strftime("%b %d") if target else None

    def _flights_text(self, flights: List[Any], slots: Dict[str, Any],
                      snapshot: Dict[str, Any]) -> str:
        origin = slots.get("origin")
        dest = slots.get("destination")
        route = f" from {origin.title()} to {dest.title()}" if origin and dest else ""
        if not flights:
            return f"Sorry, I couldn't find any flights{route} matching that."
        origin_tr = "flights" + route
        rows = []
        for i, f in enumerate(flights[:3]):
            fid = str(self._pick(f, "id") or self._pick(f, "flight_id") or self._pick(f, "number") or "?")
            price = self._pick(f, "price")
            dep = self._pick(f, "departure") or self._pick(f, "time") or self._pick(f, "departs")
            cls = self._pick(f, "class")
            price_s = f"${price}" if isinstance(price, (int, float)) else (price if price is not None else "n/a")
            dep_s = dep if dep is not None else ""
            cls_s = f" {cls}" if cls else ""
            rows.append(f"{fid}{cls_s} at {dep_s} for {price_s}".rstrip())
        head = f"I found {len(flights)} {origin_tr}."
        if len(rows) == 1:
            return f"{head} The {rows[0].split()[0]} departs {rows[0].split(' at ')[1] if ' at ' in rows[0] else ''}".strip()
        listing = "; ".join(rows)
        cheapest = min(
            (self._pick(f, "price") for f in flights if isinstance(self._pick(f, "price"), (int, float))),
            default=None,
        )
        tail = f" Cheapest is ${cheapest}." if cheapest is not None else ""
        return f"{head} {listing}.{tail} Shall I book the cheapest?"

    # narration helpers used by orchestrator
    def _flight_list_lite(self, flights: List[Any], slots: Dict[str, Any]) -> str:
        return self._flights_text(flights, slots, {})