"""Lightweight, deterministic NLU.

Why not an LLM inside the agent?
  - The 120s wall-clock cap per scenario makes remote inference risky.
  - Argument-extraction / snapshot accuracy reward determinism.
  - The theme's focus is orchestration (fast/slow path, cancellation,
    latency hiding, multimodal grounding), not language-model quality.
The NLU here is a pluggable front-end: swap `NLU` for an LLM-backed
implementation later without touching the orchestrator.

Responsibilities:
  - intent detection (manifest-tool driven + generic fallback)
  - slot extraction: spatial, temporal, numeric, flight ids, preferences
  - disfluency / self-repair segmentation (slow segments override earlier
    conflicting slots — the mechanism behind localized slot corrections)
  - hypothetical intent bandit for unseen tools
"""
from __future__ import annotations

import datetime as _dt
import re
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Tuple

from .utils import norm_keep_apos

# --------------------------------------------------------------------------
# Lexical data
# --------------------------------------------------------------------------

CITY_ALIASES: Dict[str, str] = {}
_RAW_CITIES = [
    "paris", "london", "new york", "los angeles", "san francisco", "seattle",
    "chicago", "boston", "miami", "atlanta", "dallas", "houston", "denver",
    "phoenix", "washington", "philadelphia", "portland", "orlando",
    "seoul", "tokyo", "osaka", "kyoto", "nagoya", "fukuoka", "shenzhen",
    "beijing", "shanghai", "hong kong", "taipei", "singapore", "bangkok",
    "kuala lumpur", "jakarta", "manila", "ho chi minh", "hanoi",
    "mumbai", "delhi", "bangalore", "hyderabad", "chennai", "kolkata",
    "new delhi", "ahmedabad", "pune", "dubai", "abu dhabi", "doha", "riyadh",
    "jeddah", "kuwait", "tel aviv", "istanbul", "ankara",
    "berlin", "munich", "frankfurt", "hamburg", "cologne", "stuttgart",
    "dusseldorf", "vienna", "zurich", "geneva", "basel", "milan", "rome",
    "venice", "florence", "naples", "madrid", "barcelona", "valencia",
    "seville", "lisbon", "porto", "amsterdam", "brussels", "rotterdam",
    "copenhagen", "oslo", "stockholm", "helsinki", "warsaw", "krakow",
    "prague", "budapest", "bucharest", "athens", "dublin", "edinburgh",
    "glasgow", "manchester", "birmingham", "liverpool", "leeds", "canberra",
    "sydney", "melbourne", "brisbane", "perth", "adelaide", "auckland",
    "wellington", "christchurch", "mexico city", "bogota", "lima",
    "santiago", "buenos aires", "sao paulo", "rio de janeiro", "lagos",
    "nairobi", "johannesburg", "cape town", "cairo", "caseblanca", "tunis",
    "algiers", "beirut", "amman", "muscat",
]
_CITY_LEMMAS: Dict[str, Tuple[str, ...]] = {}
for _c in _RAW_CITIES:
    _CITY_LEMMAS[_c] = tuple(w for w in _c.split() if w)

for _a, _base in {
    "nyc": "new york", "sf": "san francisco", "l.a.": "los angeles",
    "la": "los angeles", "dc": "washington", "são paulo": "sao paulo",
    "bombay": "mumbai", "prague": "prague", "uk": "london",
    "uae": "dubai", "hk": "hong kong", "pekin": "beijing",
    "stockolm": "stockholm",
}.items():
    CITY_ALIASES[_a] = _base

_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday",
             "friday", "saturday", "sunday"]
_MONTHS = ["january", "february", "march", "april", "may", "june",
           "july", "august", "september", "october", "november", "december"]
_MONTH_ABBREV = {m[:3]: i for i, m in enumerate(_MONTHS, start=1)}
for i, m in enumerate(_MONTHS, start=1):
    _MONTH_ABBREV[m] = i

_WORD_NUM = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "a": 1, "an": 1, "single": 1, "one": 1, "couple": 2, "pair": 2,
    "two": 2, "three": 3, "four": 4, "five": 5,
}

# Time-of-day buckets that map to friendly labels.
_TOD_LABELS = {"morning": "morning", "afternoon": "afternoon",
               "evening": "evening", "night": "night", "noon": "noon",
               "midnight": "midnight"}

_REPAIR_RE = re.compile(
    r"(?P<marker>"
    r"correction(?:s)?\b|"
    r"\bsorry\b|"
    r"\bsorry,\b|"
    r"\bi\s+mean\b|"
    r"\bwait(?:,)?\b|"
    r"\bhold\s+on\b|"
    r"\brather\b|"
    r"\binstead(?:\s+of)?\b|"
    r"\bno(?:,)?\s+(?:actually|sorry|wait|hold|no|not|instead)\b|"
    r"\bactually\b|"
    r"\bno\s+n[oou]?\s*$|"
    r"\bhmm\b"
    r")",
    re.IGNORECASE,
)

_FLIGHT_RE = re.compile(r"\b([A-Z]{2}\s?\d{1,4})\b")
_PRICE_RE = re.compile(r"(?:under|less than|below|max(?:imum)? of?)\s*[$€£]?\s*(\d{2,5})",
                       re.IGNORECASE)
_PAX_RE = re.compile(r"\b(\d{1,2})\s*(?:adult|child|passenger|traveler|traveller|ticket)s?\b",
                     re.IGNORECASE)

DEFAULT_DATE_FMT = "%Y-%m-%d"


# --------------------------------------------------------------------------
# Temporal resolution
# --------------------------------------------------------------------------

class TemporalResolver:
    def __init__(self, today: _dt.date = None):
        self.today = today or _dt.date.today()

    def _next_weekday(self, name: str, allow_today: bool = False) -> _dt.date:
        idx = _WEEKDAYS.index(name)
        cur = self.today.weekday()  # 0=Monday
        delta = (idx - cur) % 7
        if delta == 0 and not allow_today:
            delta = 7
        return self.today + _dt.timedelta(days=delta)

    def resolve(self, text: str) -> Optional[_dt.date]:
        t = norm_keep_apos(text)
        t = t.replace("the day after tomorrow", "day after tomorrow")

        # Absolute: yyyy-mm-dd
        m = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", t)
        if m:
            try:
                return _dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                pass
        # Absolute: mm/dd or mm/dd/yyyy (avoid matching 24h times like 09/07 weirdness)
        m = re.search(r"(?<![\d:])\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", t)
        if m:
            mm, dd = int(m.group(1)), int(m.group(2))
            yy = int(m.group(3)) if m.group(3) else self.today.year
            if 1 <= mm <= 12 and 1 <= dd <= 31 and 1900 <= yy <= 2100:
                try:
                    return _dt.date(yy, mm, dd)
                except ValueError:
                    pass
        # "Mar 5", "March 5", "5th of March", "5 March"
        m = re.search(
            r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|"
            r"january|february|march|april|june|july|august|september|"
            r"october|november|december)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?"
            r"(?:,?\s*(\d{4}))?\b",
            t,
        )
        if m:
            mo = _MONTH_ABBREV.get(m.group(1)[:3])
            day = int(m.group(2))
            yy = int(m.group(3)) if m.group(3) else self.today.year
            try:
                return _dt.date(yy, mo, day)
            except ValueError:
                pass
        m = re.search(
            r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*"
            r"(?:,?\s*(\d{4}))?\b",
            t,
        )
        if m:
            day = int(m.group(1))
            mo = _MONTH_ABBREV.get(m.group(2)[:3])
            yy = int(m.group(3)) if m.group(3) else self.today.year
            try:
                return _dt.date(yy, mo, day)
            except ValueError:
                pass

        # Relative
        if "day after tomorrow" in t:
            return self.today + _dt.timedelta(days=2)
        if "tomorrow" in t or "tomrrow" in t or "tmr" in t or "tmrw" in t:
            return self.today + _dt.timedelta(days=1)
        if "today" in t:
            return self.today
        if re.search(r"\bin\s+(\d{1,3})\s+days?\b", t):
            n = int(re.search(r"\bin\s+(\d{1,3})\s+days?\b", t).group(1))
            return self.today + _dt.timedelta(days=n)
        if "next week" in t:
            return self._next_weekday("monday") + _dt.timedelta(days=7)
        if "next month" in t:
            y, mo = self.today.year, self.today.month
            y2, mo2 = (y + 1, 1) if mo == 12 else (y, mo + 1)
            return _dt.date(y2, mo2, self.today.day)
        if "next year" in t:
            try:
                return _dt.date(self.today.year + 1, self.today.month, self.today.day)
            except ValueError:
                return _dt.date(self.today.year + 1, self.today.month, 28)
        m = re.search(r"\b(?:this|next|coming|following|on)\s+(" + "|".join(_WEEKDAYS) + r")\b", t)
        if m:
            return self._next_weekday(m.group(1), allow_today=True)
        m = re.search(r"\b(" + "|".join(_WEEKDAYS) + r")\b", t)
        if m:
            return self._next_weekday(m.group(1))
        if re.search(r"\bweekend\b", t):
            return self._next_weekday("saturday")
        return None

    @staticmethod
    def fmt(d: _dt.date) -> str:
        return d.strftime(DEFAULT_DATE_FMT)


def resolve_time(text: str) -> Optional[str]:
    """Return a 24h 'HH:MM' or a time-of-day label, or None."""
    t = norm_keep_apos(text)
    if "noon" in t:
        return "noon"
    if "midnight" in t:
        return "midnight"
    for lab in ("morning", "afternoon", "evening", "night"):
        if lab in t:
            return lab
    # 7 pm / 7pm / 7:30 pm / 19:30
    m = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", t)
    if m:
        h = int(m.group(1)); mi = int(m.group(2) or 0)
        ap = m.group(3)
        if ap == "pm" and h < 12:
            h += 12
        if ap == "am" and h == 12:
            h = 0
        if 0 <= h <= 23 and 0 <= mi <= 59:
            return f"{h:02d}:{mi:02d}"
    m = re.search(r"\b(\d{1,2}):(\d{2})\b", t)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        if 0 <= h <= 23 and 0 <= mi <= 59:
            return f"{h:02d}:{mi:02d}"
    return None


# --------------------------------------------------------------------------
# Entity extraction helpers
# --------------------------------------------------------------------------

def split_correction_segments(text: str) -> List[Dict[str, Any]]:
    """Split on self-repair markers.

    Returns a list of segments, each {'text': str, 'repair': bool}.
    Segments before the first repair marker carry the ORIGINAL intent;
    segments after it carry the CORRECTED one and override the slots they
    mention (localized slot correction).
    """
    parts = _REPAIR_RE.split(text or "")
    junk = re.compile(r"^[\s\.,!?;:'\"()\-_…~]+$")
    low_filler = {"sorry", "wait", "hold", "hmm", "i", "mean", "no", "actually",
                  "rather", "instead", "correction", "on", "uh", "um", "so"}
    segments: List[Dict[str, Any]] = []
    correction_active = False
    for part in parts:
        part = (part or "").strip(" .,!?;:'\"()-_…~\n\t")
        if not part or junk.fullmatch(part):
            continue
        tokens = part.split()
        if not tokens:
            continue
        if all(t.lower() in low_filler for t in tokens):
            continue
        segments.append({"text": part, "repair": correction_active})
        correction_active = True
    if not segments:
        segments = [{"text": (text or "").strip(), "repair": False}]
    return segments


def find_city(phrase: str, known: Dict[str, Tuple[str, ...]]) -> Optional[str]:
    """Match a raw phrase against known city lemmas (exact or fuzzy-prefix)."""
    p = norm_keep_apos(phrase or "").strip()
    if not p:
        return None
    if p in CITY_ALIASES:
        return CITY_ALIASES[p]
    if p in known:
        return p
    # multi-token exact (e.g. "new york")
    if p in _CITY_LEMMAS:
        return p
    for city, lemmas in known.items():
        if tuple(p.split()) == lemmas or p == city:
            return city
    # alias map also applies to known cities
    for alias, city in CITY_ALIASES.items():
        if p == alias:
            return city
    return None


_SPATIAL_RE = [
    re.compile(r"\bfrom\s+([a-z][\w .'’-]{1,40}?)\s+(?:to|until|upto|up\s+to)\s+([a-z][\w .'’-]{1,40})(?=\s|$)", re.I),
    re.compile(r"\bto\s+([a-z][\w .'’-]{1,40}?)\s+from\s+([a-z][\w .'’-]{1,40})(?=\s|$)", re.I),
    re.compile(r"\bfrom\s+([a-z][\w .'’-]{1,40}?)(?=\s|$)", re.I),
    re.compile(r"\bto\s+([a-z][\w .'’-]{1,40})(?=\s|$)", re.I),
    re.compile(r"\b(heading|going|flying|traveling|travelling|for|in)\s+to\s+([a-z][\w .'’-]{1,40})(?=\s|$)", re.I),
]


def _city_trim(phrase: str, known: Dict[str, Tuple[str, ...]]) -> Optional[str]:
    """Best-effort city match, trimming trailing junk words
    (e.g. 'tokyo tomorrow' -> 'tokyo', 'los angeles' stays intact)."""
    p = norm_keep_apos(phrase or "").strip()
    while p:
        c = find_city(p, known)
        if c:
            return c
        if " " in p:
            p = p.rsplit(" ", 1)[0]
        else:
            return None
    return None


def extract_spatial(text: str, known: Dict[str, Tuple[str, ...]]) -> Tuple[Optional[str], Optional[str]]:
    """Return (origin, destination) best-guess from 'from X to Y' phrasing."""
    t = norm_keep_apos(text)
    origin = destination = None
    for pat in _SPATIAL_RE:
        m = pat.search(t)
        if not m:
            continue
        g = m.groups()
        if len(g) >= 2:
            o = _city_trim(g[0], known)
            d = _city_trim(g[1], known)
            if o and d:
                return o, d
            if o:
                origin = o
            if d:
                destination = d
        elif len(g) == 1:
            v = _city_trim(g[0], known)
            if v:
                # decide origin vs destination by preposition
                frag = m.group(0)
                if frag.startswith("from"):
                    origin = v
                elif frag.startswith("to") or " to " in frag:
                    destination = v
    return origin, destination


def extract_date(text: str, resolver: TemporalResolver) -> Optional[_dt.date]:
    return resolver.resolve(text or "")


def extract_time(text: str) -> Optional[str]:
    return resolve_time(text or "")


def extract_count(text: str, keys: Tuple[str, ...] = ("passeng", "adult", "trav", "seat", "ticket")) -> Optional[int]:
    t = norm_keep_apos(text)
    m = _PAX_RE.search(t)
    if m:
        return int(m.group(1))
    # number words that could be a count; skip articles ("a/an/single")
    # which fire spuriously in sentences like "create a ticket about..."
    num_words = [w for w in _WORD_NUM if w not in ("a", "an", "single")]
    for key in keys:
        idx = t.find(key)
        if idx == -1:
            continue
        window = t[max(0, idx - 12):idx + 14]
        for m in re.finditer(r"\b(\d{1,2})\b|" + "|".join(rf"\b{w}\b" for w in num_words), window):
            tok = m.group(0)
            if tok.isdigit():
                return int(tok)
            v = _WORD_NUM.get(tok)
            if v is not None:
                return v
    for m in re.finditer(r"for\s+(" + "|".join(num_words) + r")\b", t):
        v = _WORD_NUM.get(m.group(1))
        if v is not None:
            return v
    return None


def extract_price_limit(text: str) -> Optional[int]:
    t = norm_keep_apos(text)
    m = _PRICE_RE.search(t)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return None
    for m in re.finditer(r"[$€£]\s*(\d{2,5})", t):
        try:
            return int(m.group(1))
        except ValueError:
            return None
    return None


def extract_flight_ids(text: str) -> List[str]:
    return [m.group(1).replace(" ", "") for m in _FLIGHT_RE.finditer(text or "")]


# --------------------------------------------------------------------------
# Manifest-driven intent bandit
# --------------------------------------------------------------------------

_VERB_TOOL = {
    "find": "search", "search": "search", "show": "search", "look": "search",
    "fetch": "search", "get": "search", "list": "search", "check": "search",
    "book": "book", "reserve": "book", "order": "book", "buy": "book",
    "purchase": "book", "hold": "book", "confirm": "book", "rent": "book",
    "cancel": "cancel", "refund": "cancel", "void": "cancel", "abort": "cancel",
    "create": "create", "log": "create", "file": "create", "open": "create",
    "make": "create", "register": "create", "report": "create",
    "lookup": "lookup", "diagnos": "lookup", "troubleshoot": "lookup",
    "check": "lookup", "read": "lookup", "locate": "lookup",
}

_TOKEN_STOP = {
    "the", "a", "an", "please", "of", "from", "to", "and", "or", "for",
    "my", "i", "me", "can", "could", "would", "want", "like", "get", "with",
    "it", "this", "that", "on", "at", "in", "is", "are", "do", "does",
    "flight", "flights", "ticket", "tickets", "booking", "book",
}

_GENERIC_ARTIFACTS = {
    "flight", "flights", "flight_id", "hotel", "car", "rental", "ticket",
    "manual", "guide", "device", "appointment", "order", "reservation",
    "booking", "date",
}


class IntentBandit:
    """Learn keyword lenses per tool and rank which tool a turn maps to."""

    def __init__(self):
        self._keywords: Dict[str, set] = {}
        self._artifact_terms: Dict[str, set] = {}
        self._order: List[str] = []

    def load_manifest(self, tools: List[Dict[str, Any]]) -> None:
        for spec in tools:
            name = str(spec.get("name") or spec.get("tool") or "")
            if not name:
                continue
            name = name.replace("__", "_").replace("-", "_").lower()
            kw = set()
            art = set()
            toks = [t for t in name.split("_") if t not in _TOKEN_STOP]
            for tok in toks:
                # generic artifact nouns are recall lenses, not discriminators
                if tok in _GENERIC_ARTIFACTS:
                    art.add(tok)
                else:
                    kw.add(tok)
                    kw.add(_VERB_TOOL.get(tok, tok))
            if toks:
                art.add(toks[-1])  # noun-ish final token ("car" in rent_car)
            art.add(toks[-1] if toks else "")
            params = spec.get("params") or spec.get("parameters") or []
            for p in params:
                pname = str((p.get("name") if isinstance(p, dict) else p) or "")
                pname = pname.replace("_", " ").lower()
                if pname and len(pname) > 2:
                    for tok in pname.split():
                        if tok in _GENERIC_ARTIFACTS:
                            art.add(tok)
                        elif len(tok) > 2:
                            kw.add(tok)
            # param enums are powerful lenses for slot grounding
            for p in params:
                if isinstance(p, dict):
                    for en in p.get("enum") or p.get("options") or []:
                        kw.add(str(en).lower().replace("_", " "))
            self._keywords[name] = kw
            self._artifact_terms[name] = art
            if name not in self._order:
                self._order.append(name)

    def known_tools(self) -> List[str]:
        return list(self._order)

    def score(self, tool: str, text: str) -> float:
        tok_set = set(norm_keep_apos(text).split())
        kw = self._keywords.get(tool, set())
        art = self._artifact_terms.get(tool, set())
        hits = sum(1 for k in kw if k in text or any(k in w for w in tok_set))
        tok_len = max(len(tok_set), 1)
        score = hits / max(len(kw), 1) * 2.0 + hits / tok_len
        # artifact bonus: noun in utterance (flight in "need a flight")
        tok_text = " " + text.lower() + " "
        for a in art:
            if a and (" " + a + " ") in tok_text:
                score += 0.8
        return score

    def best(self, text: str, min_score: float = 0.6) -> Optional[str]:
        if not self._order:
            return None
        ranked = sorted(
            ((self.score(t, text), t) for t in self._order),
            key=lambda x: -x[0],
        )
        top, tool = ranked[0]
        if top <= 0 or top < min_score:
            return None
        if len(ranked) > 1 and top < ranked[1][0] * 1.6:
            return None  # ambiguous: close calls between two tools
        return tool


# --------------------------------------------------------------------------
# Public NLU front-end
# --------------------------------------------------------------------------

class NLU:
    """Stateless-within-snapshot parser. Feed it manifest tool specs, call
    `parse_turn` per committed turn (works on corrected segments)."""

    def __init__(self, tool_names: Optional[List[str]] = None,
                 today: Optional[_dt.date] = None):
        self.bandit = IntentBandit()
        self.resolver = TemporalResolver(today or _dt.date.today())
        self.known_cities: Dict[str, Tuple[str, ...]] = dict(_CITY_LEMMAS)
        if tool_names:
            for t in tool_names:
                self.bandit.load_manifest([{"name": t}])

    def load_manifest(self, tools: List[Dict[str, Any]]) -> None:
        self.bandit.load_manifest(tools)
        for t in tools:
            for p in (t.get("params") or t.get("parameters") or []):
                if isinstance(p, dict):
                    for en in (p.get("enum") or p.get("options") or []):
                        if isinstance(en, str) and " " not in en:
                            self.known_cities.setdefault(en.lower(), (en.lower(),))
                            self.known_cities.setdefault(en.title(), (en.lower(),))

    def set_today(self, today: _dt.date) -> None:
        self.resolver.today = today

    def scan_cities(self, text: str) -> List[str]:
        """Every known city anywhere in the text (longest match first)."""
        t = norm_keep_apos(text or "").lower()
        found: List[str] = []

        def _hit(candidate: str) -> bool:
            import re as _re
            return bool(_re.search(r"\b" + _re.escape(candidate) + r"\b", t))

        for alias, city in sorted(CITY_ALIASES.items(), key=lambda kv: -len(kv[0])):
            if _hit(alias) and city not in found:
                found.append(city)
        for city in sorted(self.known_cities, key=len, reverse=True):
            if _hit(city) and city not in found:
                found.append(city)
        return found

    # -- intent -------------------------------------------------------------
    def detect_intent(self, text: str) -> Optional[str]:
        t = norm_keep_apos(text or "")
        # Deterministic verb anchors: cancellation/booking verbs must not lose
        # to an ambiguous noun-score against other manifest tools.
        if re.search(r"\bcancel\w*\b", t) or \
                re.search(r"\b(refund|void|abort|scrap)\b", t):
            for name in self.bandit.known_tools():
                if "cancel" in name:
                    return name
        if re.search(r"\b(?:book|reserve|purchase|buy|order|hold|confirm)\w*\b", t):
            for name in self.bandit.known_tools():
                if any(k in name for k in ("book", "reserve", "purchase",
                                           "buy", "hold")):
                    return name
        # Live flight tracking: a flight/departure word or explicit flight id
        # plus a status verb routes to flight_status. Kept narrow so "baggage
        # delay policy" (no flight id / no flight word) still hits knowledge.
        if re.search(r"\b[a-z]{1,3}\d{2,4}\b", t) or \
                re.search(r"\b(flight|departure|arrival|landing)\b", t):
            if re.search(
                    r"\b(status|on[- ]?time|delayed|delay|boarding|departed|"
                    r"arrived|cancelled|at the gate|track(?:ing)?)\b", t):
                for name in self.bandit.known_tools():
                    if "status" in name:
                        return name
        # aviation-knowledge questions (policy / rules / how-to) must route to
        # the RAG-backed knowledge tool rather than a booking tool.
        if re.search(
                r"\b(polic(?:y|ies)|rule[s]?|regulations?|allowed|permitted|"
                r"carry[- ]?on|baggage|luggage|liquids?|tsa|security|screening|"
                r"allowance|limit[s]?|refundable|fare rules|miles|upgrade|"
                r"airport code[s]?|visa|passport)\b"
                r"|\b(can|could|am)\s+i\s+(bring|take|carry|check|pack|get)\b"
                r"|\bdo\s+i\s+need\b"
                r"|\bwhat['’ ]?s?\s+(the|is)\s+(carry|baggage|luggage|refund|"
                r"policy|rule)\b|\bhow\s+(many|much|big|often|long)\b", t):
            for name in self.bandit.known_tools():
                if "knowledge" in name or "qa" in name or "explain" in name \
                        or "kb_" in name:
                    return name
        return self.bandit.best(t)

    # -- slots --------------------------------------------------------------
    def extract_slots(self, text: str) -> Dict[str, Any]:
        """Extract every grounded slot value from a single (already
        segmented) utterance. Non-contradicting, pure extraction."""
        t = text or ""
        slots: Dict[str, Any] = {}

        origin, dest = extract_spatial(t, self.known_cities)
        if origin:
            slots["origin"] = origin
        if dest:
            slots["destination"] = dest
        elif not origin:
            # Correction bias: a lone city mention in an override-style
            # utterance ("actually make that Tokyo") updates the destination.
            cities = self.scan_cities(t)
            if len(cities) == 1:
                slots["destination"] = cities[0]

        d = extract_date(t, self.resolver)
        if d:
            slots["date"] = TemporalResolver.fmt(d)

        tm = extract_time(t)
        if tm:
            if tm in _TOD_LABELS:
                slots["timeofday"] = tm
            else:
                slots["time"] = tm

        cnt = extract_count(t)
        if cnt is not None:
            slots["passengers"] = cnt

        price = extract_price_limit(t)
        if price is not None:
            slots["max_price"] = price

        flights = extract_flight_ids(t)
        if flights:
            slots["flight_ids"] = flights

        t2 = norm_keep_apos(t)
        if re.search(r"\b(cheapest|cheap|lowest price|best price|budget)\b", t2):
            slots["prefer_cheapest"] = True
        if re.search(r"\b(one[- ]?way)\b", t2):
            slots["trip_type"] = "one_way"
        if re.search(r"\b(round[- ]?trip|return)\b", t2):
            slots["trip_type"] = "round_trip"
        if re.search(r"\b(non[- ]?stop|direct)\b", t2):
            slots["nonstop"] = True
        if re.search(r"\b(business)\b", t2):
            slots["class"] = "business"
        elif re.search(r"\b(first[- ]?class)\b", t2):
            slots["class"] = "first"
        elif re.search(r"\b(economy)\b", t2):
            slots["class"] = "economy"

        # device model heuristic for manual/troubleshooting
        m = re.search(r"\b(galaxy|iphone|ipad|macbook|mac|airtv|tv|refrigerator|fridge|washer|washing machine|dryer|oven|microwave|dishwasher|ac|air conditioner|robot vacuum|vacuum|battery|buds|watch|monitor|speaker)\b", t2)
        if m:
            device = m.group(1).replace("_", " ")
            slots.setdefault("device_model", device.title())

        # free-text description / subject for ticket-creation style tools
        verb = re.search(
            r"\b(report|log|file|create|open|register|add)\s+"
            r"(?:(?:a|an|one)\s+)?(?:support|service|help|bug|tech)?\s*ticket"
            r"\s*(?:about|for|regarding|re|on)?\s*(.{3,})", t2)
        if verb and len(verb.group(2).strip()) >= 3:
            slots.setdefault("subject", verb.group(2).strip().capitalize())
        return slots

    def parse_turn(self, text: str) -> Dict[str, Any]:
        """Full turn parse with self-repair segmentation.

        Returns:
          {'intent': str|None, 'segments': [...], 'slots': {...},
           'repairs': bool, 'corrected': bool}
        Repaired segments take precedence: later segments override the
        slots they explicitly mention (localized slot correction).
        """
        segments = split_correction_segments(text or "")
        repairs = len(segments) > 1
        intent = None
        slots: Dict[str, Any] = {}
        corrected = repairs
        for i, seg in enumerate(segments):
            seg_text = seg["text"]
            seg_intent = self.detect_intent(seg_text)
            if seg_intent:
                intent = seg_intent
            # collect explicit mentions per segment
            seg_slots = self.extract_slots(seg_text)
            for k, v in seg_slots.items():
                slots[k] = v  # later (repaired) segments override
        return {
            "intent": intent,
            "segments": segments,
            "slots": slots,
            "repairs": repairs,
            "corrected": corrected,
        }