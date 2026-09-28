"""Mock environment.

Acts as the *external* tool executor side of the loop: it consumes the
agent's `tool_call`/`cancel` actions and asynchronously produces
`tool_result` events with deterministic latency and fault injection.

This mirrors the evaluation kit's "Mock Environment" for flight search,
booking, ticket creation, and frame-grounded manual lookups.
"""
from __future__ import annotations

import asyncio
import base64
import io
import math
import random
import struct
import wave
import zlib
from typing import Any, Dict, List, Optional

FLIGHT_DB = [
    {"id": "SU450", "origin": "paris", "destination": "london", "date": "2026-09-24",
     "time": "07:45", "price": 89, "class": "economy"},
    {"id": "SU451", "origin": "paris", "destination": "london", "date": "2026-09-24",
     "time": "11:30", "price": 125, "class": "economy"},
    {"id": "SU452", "origin": "paris", "destination": "london", "date": "2026-09-24",
     "time": "19:10", "price": 164, "class": "business"},
    {"id": "SU460", "origin": "berlin", "destination": "london", "date": "2026-09-24",
     "time": "08:15", "price": 112, "class": "economy"},
    {"id": "SU461", "origin": "berlin", "destination": "london", "date": "2026-09-24",
     "time": "16:40", "price": 149, "class": "economy"},
    {"id": "SU462", "origin": "berlin", "destination": "london", "date": "2026-09-24",
     "time": "21:05", "price": 178, "class": "business"},
    {"id": "SU453", "origin": "paris", "destination": "tokyo", "date": "2026-09-24",
     "time": "09:20", "price": 640, "class": "economy"},
    {"id": "SU454", "origin": "paris", "destination": "tokyo", "date": "2026-09-24",
     "time": "22:05", "price": 712, "class": "business"},
    {"id": "SU470", "origin": "new york", "destination": "london", "date": "2026-09-24",
     "time": "18:30", "price": 310, "class": "economy"},
    {"id": "SU471", "origin": "new york", "destination": "london", "date": "2026-09-24",
     "time": "23:15", "price": 405, "class": "business"},
]

DEFAULT_TRAVEL_DATE = "2026-09-24"


def make_wav(seconds: float = 1.2, sr: int = 16000, tone_hz: float = 220.0,
             amplitude: float = 0.5) -> bytes:
    n = int(sr * seconds)
    frames = bytearray()
    for i in range(n):
        t = i / sr
        env = 0.4 + 0.6 * abs(math.sin(2 * math.pi * 3 * t))
        s = amplitude * env * (0.6 * math.sin(2 * math.pi * tone_hz * t)
                               + 0.3 * math.sin(2 * math.pi * 2 * tone_hz * t))
        frames += struct.pack("<h", int(max(-1, min(1, s)) * 32767))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(bytes(frames))
    return buf.getvalue()


def make_png(w: int = 640, h: int = 480, seed: int = 0) -> bytes:
    w = max(1, int(w))
    h = max(1, int(h))

    def chunk(tag: bytes, data: bytes) -> bytes:
        c = tag + data
        return struct.pack(">I", len(data)) + c \
            + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    rows = bytearray()
    for y in range(h):
        rows.append(0)
        for x in range(w):
            v = (x * 2654435761 + y * 40503 + seed * 7919) & 0xFF
            rows += bytes([v, (v * 3) & 0xFF, (v * 5) & 0xFF])
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(bytes(rows), 6))
            + chunk(b"IEND", b""))


class MockEnv:
    """Deterministic tool executor with latency + fault injection.
    Grace semantics: a cancelled call that was about to complete still lands
    a result so the agent can salvage it (mirrors the kit's behaviour)."""

    def __init__(self, latency_ms: int = 650, fail_rate: float = 0.0,
                 rng_seed: int = 7, clock=None):
        self.latency_ms = latency_ms
        self.fail_rate = fail_rate
        self.rng = random.Random(rng_seed)
        self.clock = clock
        self.pending: Dict[str, asyncio.Task] = {}
        self.cancelled: set = set()
        self.calls: List[Dict[str, Any]] = []
        self.faults: Dict[str, bool] = {}
        self.extra_delay: Dict[str, int] = {}
        self.bookings: List[Dict[str, Any]] = []
        self.cancelled_bookings: List[Dict[str, Any]] = []
        self.tickets: List[Dict[str, Any]] = []
        self.active = 0

    # -- fault/latency injection ------------------------------------------------
    def inject_fault(self, call_id: str) -> None:
        self.faults[call_id] = True

    def inject_delay(self, call_id: str, ms: int) -> None:
        self.extra_delay[call_id] = ms

    # -- dispatch ----------------------------------------------------------------
    async def on_tool_call(self, action: Dict[str, Any], emit) -> None:
        d = action.get("data", {})
        call_id = str(d.get("call_id") or "")
        tool = str(d.get("tool") or "")
        args = d.get("args") or {}
        self.calls.append({"call_id": call_id, "tool": tool, "args": args,
                           "ts": action.get("ts", 0.0),
                           "speculative": bool(d.get("speculative")),
                           "ok": True, "error": None})
        lat = self.extra_delay.pop(call_id, 0) or self._latency_for(tool)
        fail = self.faults.pop(call_id, False) or self.rng.random() < self.fail_rate
        self.active += 1
        task = asyncio.create_task(self._execute(call_id, tool, args, lat, fail, emit))
        self.pending[call_id] = task
        task.add_done_callback(lambda _t: self._dec_active())

    def _dec_active(self) -> None:
        if self.active > 0:
            self.active -= 1

    async def on_cancel(self, action: Dict[str, Any], emit) -> None:
        for cid in (action.get("data", {}).get("call_ids") or []):
            self.cancelled.add(cid)
            task = self.pending.get(cid)
            if task and not task.done():
                task.cancel()

    # -- execution -----------------------------------------------------------------
    async def _execute(self, call_id, tool, args, latency_ms, fail, emit) -> None:
        if latency_ms > 0:
            if self.clock is not None:
                await self.clock.sleep(latency_ms / 1000.0)
            else:
                await asyncio.sleep(latency_ms / 1000.0)
        if asyncio.current_task().cancelled():
            return
        result, error, ok = None, None, True
        try:
            if fail:
                ok, error = False, "upstream_timeout"
            else:
                result = await self._run_tool(tool, args)
        except Exception as e:
            ok, error = False, str(e)
        for rec in self.calls:
            if rec["call_id"] == call_id:
                rec["ok"] = ok
                rec["error"] = error
                rec["done_ts"] = _now_ms(self.clock) / 1000.0
                break

        emit_ev = {"ts": (_now_ms(self.clock) / 1000.0), "type": "tool_result",
                   "data": {"call_id": call_id, "ok": ok,
                            "result": result, "error": error}}
        await emit(emit_ev)

    # -- tool behaviour --------------------------------------------------------------
    async def _run_tool(self, tool: str, args: Dict[str, Any]) -> Any:
        if "knowledge" in tool or "qa" in tool or "explain" in tool \
                or "kb_" in tool:
            return await self._knowledge(args)
        prov = _live_provider()
        if prov is not None:
            if any(k in tool for k in ("search", "find", "availability", "quote")):
                return await _provider_search(prov, args)
            if "cancel" in tool:
                return await _provider_cancel(prov, args)
            if any(k in tool for k in ("book", "reserve", "buy", "order", "hold")):
                return await _provider_book(prov, args)
        if "cancel" in tool:
            return self._cancel_booking(args, tool)
        if any(k in tool for k in ("manual", "diagnos")):
            return self._manual(args, tool)
        if any(k in tool for k in ("search", "find", "quote", "availability")):
            return self._search(args)
        if any(k in tool for k in ("book", "reserve", "buy", "order")):
            return self._book(args)
        if any(k in tool for k in ("ticket", "create", "log", "file")):
            return self._ticket(args)
        return {"ok": True, "result": {"note": "simulated", "echo": args}}

    async def _knowledge(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """RAG-backed aviation Q&A: retrieve from the KB, then answer (Groq
        when a key is configured, deterministic snippet otherwise)."""
        from aerupt.responder import answer_knowledge
        q = (args.get("query") or args.get("q")
             or args.get("question") or "").strip()
        out = await answer_knowledge(q)
        return {"ok": True,
                "answer": str(out.get("answer") or ""),
                "sources": out.get("sources") or []}

    def _search(self, args: Dict[str, Any]) -> Dict[str, Any]:
        origin = (args.get("origin") or "").lower().strip()
        destination = (args.get("destination") or "").lower().strip()
        when = args.get("date") or DEFAULT_TRAVEL_DATE
        cls = (args.get("class") or "").lower()
        max_price = args.get("max_price")
        results = FLIGHT_DB
        if origin:
            results = [f for f in results if f.get("origin") == origin]
        if destination:
            results = [f for f in results if f.get("destination") == destination]
        if when:
            results = [f for f in results if f.get("date") == when]
        if cls:
            results = [f for f in results if (f.get("class") or "").lower() == cls]
        if isinstance(max_price, (int, float)):
            results = [f for f in results if f.get("price", 0) <= max_price]
        sorted_res = sorted(results, key=lambda f: f.get("price", float("inf")))
        return {"ok": True, "flights": sorted_res, "count": len(sorted_res)}

    def _book(self, args: Dict[str, Any]) -> Dict[str, Any]:
        fid = args.get("flight_id") or args.get("flight") or args.get("id")
        for b in self.bookings:
            if b.get("flight_id") == fid:
                return {"ok": False, "error": "duplicate_booking",
                        "existing": b.get("reference")}
        ref = f"AER{1000 + len(self.bookings) + 1}"
        self.bookings.append({"flight_id": fid, "reference": ref,
                              "passengers": args.get("passengers")})
        return {"ok": True, "booking": {"flight_id": fid, "reference": ref},
                "flight_id": fid, "reference": ref}

    def _cancel_booking(self, args: Dict[str, Any], tool: str) -> Dict[str, Any]:
        fid = args.get("flight_id") or args.get("flight") or args.get("id")
        ref = args.get("reference")
        for b in self.bookings:
            if (ref and b.get("reference") == ref) or \
               (fid and b.get("flight_id") == str(fid).upper()):
                if b.get("cancelled"):
                    return {"ok": False, "error": "already_cancelled",
                            "existing": b.get("reference")}
                b["cancelled"] = True
                b["reason"] = "user_request"
                self.cancelled_bookings.append(dict(b))
                return {"ok": True,
                        "cancelled": {"flight_id": b["flight_id"],
                                      "reference": b["reference"]},
                        "flight_id": b["flight_id"],
                        "reference": b["reference"]}
        return {"ok": False, "error": "no_such_booking",
                "requested": fid or ref}

    def _ticket(self, args: Dict[str, Any]) -> Dict[str, Any]:
        subject = args.get("subject") or args.get("description") or "General issue"
        tkid = f"TK{900 + len(self.tickets) + 1}"
        self.tickets.append({"id": tkid, "subject": subject})
        return {"ok": True, "ticket": {"id": tkid, "subject": subject},
                "ticket_id": tkid, "id": tkid}

    def _manual(self, args: Dict[str, Any], tool: str) -> Dict[str, Any]:
        query = str(args.get("query") or args.get("q") or args.get("subject") or "").lower()
        frame = args.get("frame_ref") or args.get("frame") or ""
        device = str(args.get("device_model") or args.get("model") or "device").lower()
        if frame:
            return {"ok": True, "frame": frame,
                    "steps": ["Power-cycle the unit by holding power for 10s.",
                              "Reset to factory defaults if the LED stays amber.",
                              f"Check the {device or 'owner'} manual page 12."]}
        if "spin" in query:
            return {"ok": True,
                    "answer": "Run a spin-only cycle after balancing the load."}
        if "power" in query or "turn" in query or "on" in query:
            return {"ok": True,
                    "answer": "Check the outlet and the rear power switch; "
                              "hold power for 10 seconds to hard-reset."}
        return {"ok": True,
                "answer": "The manual suggests power-cycling the appliance and "
                          "checking the socket first."}

    def _latency_for(self, tool: str) -> int:
        base = self.latency_ms
        if any(k in tool for k in ("book", "ticket", "create", "file")):
            base += 200
        if "manual" in tool or "diagnos" in tool:
            base += 100
        return base

    def summary(self) -> Dict[str, Any]:
        return {"calls": len(self.calls),
                "call_records": list(self.calls),
                "bookings": len(self.bookings),
                "cancelled_bookings": len(self.cancelled_bookings),
                "tickets": len(self.tickets),
                "cancelled_ids": sorted(self.cancelled)}


def _now_ms(clock) -> float:
    import time
    return (clock.now() if clock else time.monotonic()) * 1000.0


# ---------------------------------------------------------------------------
# Optional live-platform routing (Amadeus). Without credentials, or when
# AERUPT_PLATFORM isn't "amadeus", the in-repo sandbox is used — zero behavior
# change for the harness evaluation.
# ---------------------------------------------------------------------------
def _live_provider():
    import os
    from aerupt.providers import active_provider
    if os.environ.get("AERUPT_PLATFORM", "").strip().lower() != "amadeus":
        return None
    p = active_provider()
    if p.name != "amadeus" or not getattr(p, "configured", False):
        return None
    return p


async def _provider_search(prov, args: Dict[str, Any]) -> Dict[str, Any]:
    return await prov.search_flights(
        origin=args.get("origin"), destination=args.get("destination"),
        date=args.get("date"), cabin_class=args.get("class"),
        max_price=args.get("max_price"))


async def _provider_book(prov, args: Dict[str, Any]) -> Dict[str, Any]:
    return await prov.book_flight(
        flight_id=args.get("flight_id") or args.get("id"),
        passengers=int(args.get("passengers") or 1),
        cabin_class=args.get("class"))


async def _provider_cancel(prov, args: Dict[str, Any]) -> Dict[str, Any]:
    return await prov.cancel_booking(
        flight_id=args.get("flight_id") or args.get("id"),
        reference=args.get("reference"))