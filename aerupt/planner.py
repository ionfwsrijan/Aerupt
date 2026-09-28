"""Slow-path planning: turn goals -> tool-call chains.

Converts a parsed turn into a `Plan` of executable steps, mapping extracted
slots onto the manifest's parameter schemas (with enum checking), deciding
when to chain (search -> book), when results from earlier turns/session can
satisfy a step (result reuse), and when a targeted clarification is needed
instead of guessing (safety + quality).
"""
from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional

from .nlu import NLU
from .tools import ToolRegistry, ToolSpec

MISSING = object()
_PREF_KEYS = ("search", "find", "look", "fetch", "get", "list", "quote")
_BOOK_KEYS = ("book", "reserve", "buy", "purchase", "order", "hold", "confirm")
_CREATE_KEYS = ("create", "log", "file", "open", "register", "make")
_LOOKUP_KEYS = ("lookup", "diagnos", "troubleshoot", "check")


class Step:
    def __init__(self, seq: int, tool: str, build: Callable[[Dict], Any],
                 deps: Optional[List[int]] = None, on_result: Optional[Callable] = None):
        self.seq = seq
        self.tool = tool
        self.build = build
        self.deps = list(deps or [])
        self.on_result = on_result


class Plan:
    def __init__(self, steps: List[Step], missing: Optional[List[Dict[str, Any]]] = None,
                 note: str = ""):
        self.steps = steps
        self.missing = missing or []
        self.note = note

    @property
    def done(self) -> bool:  # placeholder, real check lives in orchestrator
        return False


def _has_verb(text: str) -> bool:
    t = text.lower()
    return any(k in t for k in _PREF_KEYS) or any(k in t for k in _BOOK_KEYS) \
        or any(k in t for k in _CREATE_KEYS) or any(k in t for k in _LOOKUP_KEYS) \
        or "troubleshoot" in t or "figure" in t


def split_clauses(text: str) -> List[str]:
    t = (text or "").strip()
    if not t:
        return [t]
    tl = t.lower()
    for sep in (" and then ", " then ", " and afterwards ", ", then "):
        if sep in tl:
            left = tl.split(sep, 1)[0]
            right = tl.split(sep, 1)[1]
            if _has_verb(left) and _has_verb(right):
                return [t[:len(left)].strip(), t[len(tl) - len(right):].strip()]
    for sep in (" and ", ", "):
        raw = tl.split(sep)
        clean = [i for i, p in enumerate(raw) if _has_verb(p)]
        if len(clean) >= 2:
            cut = clean[1]
            return [" ".join(x for x in raw[:cut]).strip(),
                    " ".join(x for x in raw[cut:]).strip()]
    return [text]


_alias_slots = {
    "origin": ["origin", "departure_city", "from", "from_city"],
    "destination": ["destination", "arrival_city", "to", "to_city", "dest"],
    "date": ["date", "dt", "travel_date", "departure_date", "when", "day"],
    "time": ["time", "departure_time", "boarding_time"],
    "passengers": ["passengers", "adults", "pax", "count", "travelers", "travellers", "quantity"],
    "class": ["class", "cabin", "travel_class"],
    "flight_id": ["flight_id", "flight", "flights", "id", "number", "flight_number"],
    "query": ["query", "q", "question", "issue"],
    "subject": ["subject", "topic", "summary", "title"],
    "description": ["description", "details", "desc", "problem", "error", "symptom"],
    "device_model": ["device_model", "model", "device"],
    "max_price": ["max_price", "price_limit", "budget", "max", "price"],
    "frame": ["frame", "image", "image_ref", "frame_ref", "photo", "frame_data"],
    "trip_type": ["trip_type", "journey_type", "route_type"],
    "nonstop": ["nonstop", "direct", "non_stop"],
}

_ColorWords = {"red", "blue", "green", "black", "white", "silver", "grey",
               "gray", "gold", "yellow", "orange", "purple", "pink", "brown",
               "navy", "beige", "rose", "space gray", "space grey"}
_SizeWords = {"small", "medium", "large", "xl", "xs", "s", "m", "l", "extra large"}


class Planner:
    def __init__(self, nlu: NLU, registry: ToolRegistry):
        self.nlu = nlu
        self.registry = registry

    # -- main ---------------------------------------------------------------
    def missing_params(self, tool: str, slots: Dict[str, Any],
                       results: Optional[Dict] = None) -> List[Dict[str, Any]]:
        spec = self.registry.get(tool)
        if spec is None:
            return []
        args, missing = self.map_args(spec, slots or {}, "", results)
        return missing

    def build(self, text: str, parse: Dict[str, Any],
              snapshot: Any, session: Any) -> Plan:
        slots = dict(snapshot.slots)
        slots.update(parse.get("slots") or {})
        slots.setdefault("_user_text", text or "")
        clauses = split_clauses(text or "")
        clause_intents = []
        for c in clauses:
            ci = self.nlu.bandit.best(c, min_score=0.25)
            clause_intents.append(ci)
        if not any(clause_intents):
            clause_intents = [parse.get("intent")]

        tools = self._plan_tools(clause_intents)
        if not tools:
            return Plan([], note="no_intent")

        # If the speaker mixed search + book phrasing in one breath (single
        # detected intent but search vocabulary present), prefer a chain.
        saw_search_word = any(k in (text or "").lower()
                             for k in _PREF_KEYS + _LOOKUP_KEYS
                             + ("availability", "option", "rate", "quote",
                                "flight", "route", "trip", "tickets", "return"))
        if (len(tools) == 1 and self._is_book(tools[0]) and saw_search_word):
            tools = [self._first_search_tool(), tools[0]]
        search_tool = next((t for t in tools if self._is_search(t)), None)

        # Implicit search: booking without a flight id and without usable
        # session results needs a fresh search to arbitrate the flight.
        if not search_tool and len(tools) >= 1 and self._is_book(tools[-1]):
            wants_fid = bool(slots.get("flight_id") or slots.get("flight_ids")
                             or slots.get("selected_flight"))
            routes_known = bool(slots.get("origin") and slots.get("destination"))
            if not wants_fid and routes_known:
                last = getattr(session, "last_search", None) or {}
                wanted = {k: slots.get(k) for k in ("origin", "destination", "date")}
                current = {k: last.get(k) for k in ("origin", "destination", "date")}
                already = bool(session and getattr(session, "last_flights", None))
                if not (already and wanted == current):
                    tools = [self._first_search_tool(), tools[-1]]
                    search_tool = self._first_search_tool()

        steps: List[Step] = []
        seq = 0

        if len(tools) > 1 and search_tool:
            st = self.registry.get(search_tool)
            steps.append(Step(seq, search_tool,
                              build=self._search_args(st, slots, text),
                              on_result=self._store_flights))
            seq += 1

        final_tool = tools[-1]
        spec = self.registry.get(final_tool)
        if spec is None:
            return Plan([], note="unseen_tool")

        if self._is_knowledge(final_tool):
            steps.append(Step(seq, final_tool,
                              build=self._knowledge_args(text),
                              deps=[s.seq for s in steps],
                              on_result=self._generic_store(spec)))
        elif self._is_book(final_tool):
            steps.append(Step(seq, final_tool,
                              build=self._book_args(spec, slots, text, snapshot,
                                                    session, has_search=bool(steps)),
                              deps=[s.seq for s in steps],
                              on_result=self._store_booking))
        else:
            steps.append(Step(seq, final_tool,
                              build=self._generic_args(spec, slots, text, session, snapshot),
                              deps=[s.seq for s in steps],
                              on_result=self._generic_store(spec)))
        return Plan(steps, note="chain" if len(steps) > 1 else "single")

    # -- tool selection from clause intents -----------------------------------
    def _plan_tools(self, clause_intents: List[Optional[str]]) -> List[str]:
        tools: List[str] = []
        for ci in clause_intents:
            if not ci:
                continue
            if ci in self.registry.known_names():
                tools.append(ci)
        # dedupe preserving order
        out: List[str] = []
        for t in tools:
            if t not in out:
                out.append(t)
        return out

    def _first_search_tool(self) -> str:
        for name in self.registry.known_names():
            if self._is_search(name):
                return name
        return list(self.registry.known_names() or [""])[0]

    def _first_book_tool(self) -> str:
        for name in self.registry.known_names():
            if self._is_book(name):
                return name
        return list(self.registry.known_names() or [""])[0]

    @staticmethod
    def _is_search(name: str) -> bool:
        return any(k in name for k in ("search", "find", "lookup", "quote",
                                       "availability", "list", "manual"))

    @staticmethod
    def _is_book(name: str) -> bool:
        return any(k in name for k in ("book", "reserve", "buy", "order",
                                       "purchase", "hold"))

    @staticmethod
    def _is_knowledge(name: str) -> bool:
        return any(k in name for k in ("knowledge", "qa", "explain", "kb"))

    def _knowledge_args(self, text: str) -> Callable[[Dict], Any]:
        """The whole user question is the RAG query — no verb/stopword
        mangling like `_query_from_text` does for manual-tool queries."""
        def build(results: Dict) -> Any:
            q = (text or "").strip()
            if not q:
                return MISSING
            return {"query": q}
        return build

    # -- arg builders ----------------------------------------------------------
    def _search_args(self, spec: Optional[ToolSpec], slots: Dict[str, Any],
                     text: str) -> Callable[[Dict], Any]:
        def build(results: Dict) -> Any:
            args, missing = self.map_args(spec, slots, text, results)
            if missing:
                return MISSING
            return args
        return build

    def _book_args(self, spec: Optional[ToolSpec], slots: Dict[str, Any],
                   text: str, snapshot: Any, session: Any,
                   has_search: bool) -> Callable[[Dict], Any]:
        def build(results: Dict) -> Any:
            # A code is "explicit" only when the user *spoke* it this turn.
            # Session/ledger carryover ids must yield to a fresh chained search,
            # so "book to tokyo" after a london booking re-picks for tokyo.
            explicit = self._explicit_fid(text)
            fid: Optional[str] = None
            if has_search:
                search_res = results.get(0, {})
                flights = self._extract_flights(search_res)
                if flights:
                    fid = self._pick_flight(slots, flights)
            if not fid:
                fid = explicit or slots.get("flight_id") or slots.get("selected_flight")
            if not fid and session:
                flights = getattr(session, "last_flights", None) or []
                fid = self._pick_flight(slots, flights) if flights else None
            if not fid and (slots.get("flight_ids") or []):
                fid = slots["flight_ids"][0]
            if fid:
                slots["flight_id"] = fid
            args, missing = self.map_args(spec, slots, text, results)
            if fid:
                args["flight_id"] = fid
            if missing:
                return MISSING
            return args
        return build

    def _generic_args(self, spec: Optional[ToolSpec], slots: Dict[str, Any],
                      text: str, session: Any, snapshot: Any) -> Callable[[Dict], Any]:
        def build(results: Dict) -> Any:
            args, missing = self.map_args(spec, slots, text, results)
            if missing:
                return MISSING
            return args
        return build

    # -- generic slot->param mapping -------------------------------------------
    def map_args(self, spec: Optional[ToolSpec], slots: Dict[str, Any],
                 text: str, results: Optional[Dict] = None) -> tuple:
        if spec is None:
            return {}, []
        args: Dict[str, Any] = {}
        missing: List[Dict[str, Any]] = []
        required = list(spec.required)
        for p in spec.params:
            pname = (p.get("name") or "").replace("_", " ").lower()
            key = p.get("name")
            if not key:
                continue
            val = self._resolve_param(key, pname, p, slots, text, results)
            if val is MISSING or val is None:
                if key in required:
                    missing.append(p)
                continue
            if self._enum_ok(p, val):
                args[key] = val
            else:
                missing.append(p)
        for p in spec.params:
            key = p.get("name")
            if key in args:
                continue
            if p.get("default") is not None:
                args[key] = p["default"]
            elif not key in required and key not in args:
                pass
        return args, missing

    def _resolve_param(self, key: str, pname: str, p: Dict, slots: Dict[str, Any],
                       text: str, results: Optional[Dict]) -> Any:
        # 1) exact or aliased slot
        for head, aliases in _alias_slots.items():
            if key in aliases or pname in aliases or key == head:
                if head in slots and slots[head] is not None:
                    return slots[head]
                for a in aliases:
                    if a in slots and slots[a] is not None:
                        return slots[a]
        # 2) token-level product of param name vs utterance
        t = " " + (text or "").lower() + " "
        toks = [x for x in (key + " " + pname).split() if len(x) > 1]
        for tok in toks:
            if " " + tok.lower() + " " in t:
                # param keyword quoted in speech -> grab surrounding noun phrase
                ctx = self._context_phrase(t, tok.lower())
                if ctx:
                    return ctx
        # 3) semantic heuristics
        if any(w in pname for w in ("color", "colour")):
            for c in _ColorWords:
                if " " + c + " " in t:
                    return c
        if any(w in pname for w in ("size", "dimension")):
            for s in _SizeWords:
                if " " + s + " " in t:
                    return s
        # semantic heuristics: free-text query for manual/support tools
        if any(w in pname for w in ("query", "question", "issue", "description")):
            q = self._query_from_text(text)
            if q:
                return q
        # numeric params
        ptype = str(p.get("type") or "").lower()
        if ptype in ("integer", "number", "int"):
            return self._num_from(text) or MISSING
        if ptype == "boolean":
            return self._bool_from(text)
        # enum passthrough
        enum = list(p.get("enum") or p.get("options") or [])
        if enum:
            for e in enum:
                if str(e).lower() in t:
                    return e
            return MISSING
        # results passthrough (chained step feeds values in)
        if results and key in results:
            v = results[key]
            if isinstance(v, dict) and "result" in v:
                return v["result"]
        return MISSING

    @staticmethod
    def _context_phrase(text: str, tok: str) -> Optional[str]:
        # crude: return token after the keyword token
        idx = text.find(" " + tok + " ")
        if idx == -1:
            return None
        after = text[idx + len(tok) + 1:].strip()
        m = re.match(r"([a-z][\w .'’-]{1,24}?)(?:\s+(?:please|for|on|at)\b|$)", after)
        if m and m.group(1).strip():
            return m.group(1).strip().capitalize()
        return None

    @staticmethod
    def _query_from_text(text: str) -> Optional[str]:
        t = (text or "").lower()
        t = re.sub(r"^(okay|ok|so|well|hey|please)[, ]*", "", t).strip()
        t = re.sub(r"\b(check|look up|lookup|find|search|open|read|fetch|get|tell me|show me|please)\b", "", t).strip()
        t = re.sub(r"\b(the|a|an)\b", "", t).strip()
        t = re.sub(r"\b(manual|guide|docs|documentation|assistant|help)\b", "", t).strip()
        t = re.sub(r"[?.!]+", "", t).strip()
        t = re.sub(r"\s+", " ", t).strip(" :,;-")
        if any(p in t for p in ("in the manual", "on the manual", "for ")):
            for p in ("in the manual", "on the manual", "for the manual"):
                t = t.replace(p, "")
        t = t.strip(" ,;:-")
        return t.title() if 3 <= len(t) <= 120 else None

    @staticmethod
    def _num_from(text: str) -> Optional[int]:
        m = re.search(r"\b(\d{1,2})\b", text or "")
        return int(m.group(1)) if m else None

    @staticmethod
    def _bool_from(text: str) -> Any:
        t = (text or "").lower()
        if re.search(r"\b(yes|yeah|sure|please do|correct)\b", t):
            return True
        if re.search(r"\b(no|not|never)\b", t):
            return False
        return None

    @staticmethod
    def _enum_ok(p: Dict, val: Any) -> bool:
        enum = list(p.get("enum") or p.get("options") or [])
        if not enum:
            return True
        return any(str(e).lower() == str(val).lower()
                   or str(e).lower() in str(val).lower() for e in enum)

    # -- result helpers ---------------------------------------------------------
    @staticmethod
    def _extract_flights(res: Any) -> List[Any]:
        if isinstance(res, dict):
            for k in ("flights", "results", "options"):
                if isinstance(res.get(k), list):
                    return res[k]
            inner = None
            for v in res.values():
                if isinstance(v, dict):
                    inner = Planner._extract_flights(v)
                    if inner:
                        return inner
        elif isinstance(res, list):
            return res
        return []

    @staticmethod
    def _pick_flight(slots: Dict[str, Any], flights: List[Any]) -> Optional[str]:
        if not flights:
            return None
        text = slots.get("_user_text", "") or ""
        cabin = (slots.get("class") or "").lower()
        pool = flights
        if cabin in ("business", "first", "economy"):
            classed = [f for f in flights
                       if str(Planner._flight_class(f)).lower() == cabin]
            if classed:
                pool = classed
        # preference-based selection
        if any(k in text.lower() for k in ("cheapest", "cheap", "cheaper",
                                           "lowest", "budget", "best")):
            prices = [(Planner._flight_price(f), f) for f in pool]
            prices = [(p, f) for p, f in prices if isinstance(p, (int, float))]
            if prices:
                prices.sort(key=lambda x: x[0])
                return Planner._flight_id(prices[0][1])
        if any(w in text.lower() for w in ("first", "earliest", "earlier")):
            return Planner._flight_id(pool[0])
        if "last" in text.lower():
            return Planner._flight_id(pool[-1])
        for i, word in enumerate(("second", "third", "fourth")):
            if word in text.lower() and i < len(pool):
                return Planner._flight_id(pool[i + 1])
        if texts := (slots.get("flight_ids") or []):
            for f in pool:
                if Planner._flight_id(f) in texts:
                    return Planner._flight_id(f)
        prices = [(Planner._flight_price(f), f) for f in pool]
        prices = [(p, f) for p, f in prices if isinstance(p, (int, float))]
        if prices:
            prices.sort(key=lambda x: x[0])
            return Planner._flight_id(prices[0][1])
        return Planner._flight_id(pool[0])

    @staticmethod
    def _flight_class(f: Any) -> Any:
        if isinstance(f, dict):
            return f.get("class") or f.get("cabin")
        return None

    @staticmethod
    def _flight_price(f: Any) -> Any:
        if isinstance(f, dict):
            return f.get("price") or f.get("fare")
        return None

    @staticmethod
    def _flight_id(f: Any) -> Optional[str]:
        if isinstance(f, dict):
            for k in ("id", "flight_id", "number", "flight"):
                if f.get(k):
                    return str(f[k])
        return str(f)

    @staticmethod
    def _explicit_fid(text: str) -> Optional[str]:
        """Only a code the user *literally spoke* counts as an explicit pick.
        Mirror-session / ledger carryover ids must never satisfy a new route."""
        m = re.search(r"\b[A-Za-z]{1,3}\d{2,4}\b", text or "")
        return m.group(0).upper() if m else None

    # -- result transformers (attach authoritative data to session/snapshot) ----
    def _store_flights(self, result: Any, snapshot: Any, session: Any) -> None:
        flights = self._extract_flights(result)
        if flights and session is not None:
            session.last_flights = flights
        if snapshot is not None:
            snapshot.set_meta("last_flight_count", len(flights))

    def _store_booking(self, result: Any, snapshot: Any, session: Any) -> None:
        if snapshot is not None:
            fid = self._flight_id(result) if not isinstance(result, dict) else \
                (result.get("flight_id") or result.get("booking", {}).get("flight_id")
                 if isinstance(result.get("booking"), dict) else result.get("flight_id"))
            if fid:
                snapshot.set_result("flight_id", fid)

    def _generic_store(self, spec: ToolSpec) -> Callable:
        def store(result: Any, snapshot: Any, session: Any) -> None:
            if snapshot is None:
                return
            for k in ("ticket_id", "reference", "id", "booking_id"):
                if isinstance(result, dict) and result.get(k):
                    snapshot.set_result(k, result[k])
                    break
        return store