"""Flight platform provider seam.

The agent's booking workflow (`flight_search`, `book_flight`, `cancel_booking`)
talks to a *provider* that owns the real booking platform. This module defines
the provider interface + the factory.

`AERUPT_PLATFORM`:
  - "mock"    (default) — in-repo sandbox (the harness mock env).
  - "amadeus" — Amadeus Self-Service API (test sandbox by default). Requires
    `AMADEUS_CLIENT_ID` / `AMADEUS_CLIENT_SECRET`; degrades to the sandbox
    when credentials are missing.

All providers return **internal, normalized payloads** (the same shape the
planner / responder already consume), so swapping a platform never touches the
agent core.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import asyncio

DEFAULT_CITY_CODES = {
    "paris": "PAR", "london": "LON", "tokyo": "TYO", "berlin": "BER",
    "new york": "NYC", "dehli": "DEL", "delhi": "DEL", "frankfurt": "FRA",
    "amsterdam": "AMS", "singapore": "SIN", "dubai": "DXB",
    "hong kong": "HKG", "los angeles": "LAX",
}

# IATA city codes Amadeus resolves to a concrete airport cluster.
AIRPORT_CODES = {
    "CDG": "Paris", "ORY": "Paris", "LHR": "London", "LGW": "London",
    "STN": "London", "JFK": "New York", "EWR": "New York", "LGA": "New York",
    "HND": "Tokyo", "NRT": "Tokyo", "BER": "Berlin", "FRA": "Frankfurt",
    "AMS": "Amsterdam", "SIN": "Singapore", "DXB": "Dubai", "HKG": "Hong Kong",
    "LAX": "Los Angeles", "DEL": "Delhi",
}


def city_to_code(city: str) -> Optional[str]:
    if not city:
        return None
    c = str(city).strip().lower()
    if c in DEFAULT_CITY_CODES:
        return DEFAULT_CITY_CODES[c]
    for code, name in AIRPORT_CODES.items():
        if code.lower() == c:
            return code
    # exact 3-letter guess: uppercase
    if len(c) == 3 and c.isalpha():
        return c.upper()
    return None


class BookingProvider:
    """Interface. Subclasses implement real platform calls; every method
    returns the internal normalized payload shape."""

    name = "base"

    async def search_flights(self, origin: str, destination: str, date: str,
                             cabin_class: Optional[str] = None,
                             max_price: Optional[float] = None) -> Dict[str, Any]:
        return {"ok": True, "flights": [], "count": 0, "provider": self.name}

    async def book_flight(self, flight_id: str, passengers: int = 1,
                          cabin_class: Optional[str] = None) -> Dict[str, Any]:
        return {"ok": False, "error": "unimplemented"}

    async def cancel_booking(self, flight_id: Optional[str] = None,
                             reference: Optional[str] = None) -> Dict[str, Any]:
        return {"ok": False, "error": "unimplemented"}


class MockBookingProvider(BookingProvider):
    """The in-repo sandbox — mirrors harness.mockenv's deterministic data so a
    no-key demo is identical to the evaluated runbook."""

    name = "mock"

    async def search_flights(self, origin: str, destination: str, date: str,
                             cabin_class: Optional[str] = None,
                             max_price: Optional[float] = None) -> Dict[str, Any]:
        from harness.mockenv import DEFAULT_TRAVEL_DATE, FLIGHT_DB
        results = FLIGHT_DB
        if origin:
            results = [f for f in results if f.get("origin") == origin.lower()]
        if destination:
            results = [f for f in results if f.get("destination") == destination.lower()]
        when = date or DEFAULT_TRAVEL_DATE
        results = [f for f in results if f.get("date") == when]
        if cabin_class:
            results = [f for f in results if (f.get("class") or "").lower() == cabin_class.lower()]
        if isinstance(max_price, (int, float)):
            results = [f for f in results if f.get("price", 0) <= max_price]
        results = sorted(results, key=lambda f: f.get("price", float("inf")))
        return {"ok": True, "flights": results, "count": len(results),
                "provider": self.name}

    async def book_flight(self, flight_id: str, passengers: int = 1,
                          cabin_class: Optional[str] = None) -> Dict[str, Any]:
        await asyncio.sleep(0)
        from harness.mockenv import FLIGHT_DB
        if not any(f.get("id") == flight_id for f in FLIGHT_DB):
            return {"ok": False, "error": "no_such_flight", "flight_id": flight_id,
                    "provider": self.name}
        seed = sum(ord(c) for c in str(flight_id)) % 900
        ref = "AER%04d" % (1001 + seed)
        return {"ok": True, "booking": {"flight_id": flight_id, "reference": ref},
                "flight_id": flight_id, "reference": ref, "provider": self.name}

    async def cancel_booking(self, flight_id: Optional[str] = None,
                             reference: Optional[str] = None) -> Dict[str, Any]:
        return {"ok": True,
                "cancelled": {"flight_id": flight_id, "reference": reference},
                "flight_id": flight_id, "reference": reference,
                "provider": self.name}


_PROVIDER_CACHE: Optional[BookingProvider] = None


def active_provider() -> BookingProvider:
    """The configured provider (mock by default; amadeus when credentials are
    set). Never raises — unknown/credential-less configs fall back cleanly."""
    global _PROVIDER_CACHE
    if _PROVIDER_CACHE is not None:
        return _PROVIDER_CACHE
    platform = os.environ.get("AERUPT_PLATFORM", "mock").strip().lower()
    if platform == "amadeus":
        from .amadeus import AmadeusProvider
        _PROVIDER_CACHE = AmadeusProvider()
    else:
        _PROVIDER_CACHE = MockBookingProvider()
    return _PROVIDER_CACHE


def reset_provider_cache() -> None:
    global _PROVIDER_CACHE
    _PROVIDER_CACHE = None