"""Amadeus Self-Service API provider.

Talks to the real flight-booking platform Amadeus (test sandbox by default:
`https://test.api.amadeus.com`). Flows:

  - `flight_search`      -> GET /v2/shopping/flight-offers
  - `book_flight`        -> POST /v1/booking/flight-orders (guest order)
  - `cancel_booking`     -> DELETE /v1/booking/flight-orders/{id}

Credentials (`AMADEUS_CLIENT_ID` / `AMADEUS_CLIENT_SECRET`) are required; the
provider reports `configured=False` without them so callers can fall back to
the sandbox. Every failure returns a normalized `{"ok": False, "error": ...}`
which the agent's honest-failure path already renders.
"""
from __future__ import annotations

import os
import time
from typing import Any, Dict, Optional

import httpx

from . import AIRPORT_CODES, BookingProvider, city_to_code

DEFAULT_BASE = "https://test.api.amadeus.com"
TIMEOUT_S = 12.0


def _city_name(code: Optional[str]) -> Optional[str]:
    return AIRPORT_CODES.get(code) if code else None


class AmadeusProvider(BookingProvider):
    name = "amadeus"

    def __init__(self) -> None:
        self.base = os.environ.get("AMADEUS_BASE_URL", DEFAULT_BASE).rstrip("/")
        self.client_id = os.environ.get("AMADEUS_CLIENT_ID") or ""
        self.client_secret = os.environ.get("AMADEUS_CLIENT_SECRET") or ""
        self._token: Optional[str] = None
        self._token_exp = 0.0
        self._offers: Dict[str, Any] = {}
        self._orders: Dict[str, Any] = {}

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    async def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.base, timeout=TIMEOUT_S)

    async def _token(self) -> Optional[str]:
        if not self.configured:
            return None
        if self._token and time.time() < self._token_exp - 30:
            return self._token
        async with await self._client() as client:
            resp = await client.post(
                "/v1/security/oauth2/token",
                data={"grant_type": "client_credentials",
                      "client_id": self.client_id,
                      "client_secret": self.client_secret})
        if resp.status_code != 200:
            return None
        data = resp.json()
        self._token = data.get("access_token")
        self._token_exp = time.time() + float(data.get("expires_in", 1800))
        return self._token

    async def _get(self, url: str, params: Dict[str, Any]) -> Dict[str, Any]:
        tok = await self._token()
        if not tok:
            return {"ok": False, "error": "amadeus_not_authenticated"}
        async with await self._client() as client:
            resp = await client.get(url, params=params,
                                    headers={"Authorization": f"Bearer {tok}"})
        return self._dispatch(resp, url)

    async def _post(self, url: str, body: Dict[str, Any]) -> Dict[str, Any]:
        tok = await self._token()
        if not tok:
            return {"ok": False, "error": "amadeus_not_authenticated"}
        async with await self._client() as client:
            resp = await client.post(url, json=body,
                                     headers={"Authorization": f"Bearer {tok}"})
        return self._dispatch(resp, url)

    @staticmethod
    def _dispatch(resp: httpx.Response, url: str) -> Dict[str, Any]:
        if resp.status_code in (200, 201):
            return {"ok": True, "data": resp.json(), "provider": "amadeus"}
        try:
            detail = resp.json()
        except Exception:
            detail = {"message": resp.text[:160]}
        err = "amadeus_{0}".format(resp.status_code)
        if isinstance(detail, dict):
            d = detail.get("errors")
            if isinstance(d, list) and d:
                err = str(d[0].get("detail") or d[0].get("title") or err)
        return {"ok": False, "error": str(err), "status": resp.status_code,
                "path": url, "provider": "amadeus"}

    # -- search -------------------------------------------------------------
    async def search_flights(self, origin: str, destination: str, date: str,
                             cabin_class: Optional[str] = None,
                             max_price: Optional[float] = None) -> Dict[str, Any]:
        from_city = city_to_code(origin)
        to_city = city_to_code(destination)
        if not from_city or not to_city:
            return {"ok": False, "error": "unknown_airport_city",
                    "origin": origin, "destination": destination,
                    "provider": self.name}
        params: Dict[str, Any] = {
            "originLocationCode": from_city,
            "destinationLocationCode": to_city,
            "departureDate": date,
            "adults": 1,
            "max": 15,
        }
        if cabin_class:
            params["travelClass"] = cabin_class.upper().replace(" ", "_")
        out = await self._get("/v2/shopping/flight-offers", params)
        if not out.get("ok"):
            return out
        return self._normalize_search(out["data"])

    def _normalize_search(self, payload: Any) -> Dict[str, Any]:
        offers = payload.get("data") if isinstance(payload, dict) else payload
        flights: list = []
        if isinstance(offers, list):
            for off in offers:
                if not isinstance(off, dict):
                    continue
                flights.append(self._offer_to_flight(off))
        for off in (offers or []) if isinstance(offers, list) else []:
            if isinstance(off, dict):
                self._offers[str(off.get("id"))] = off
        return {"ok": True, "flights": flights, "count": len(flights),
                "provider": self.name}

    def _offer_to_flight(self, off: Dict[str, Any]) -> Dict[str, Any]:
        itineraries = off.get("itineraries") or []
        seg0 = None
        last_seg = None
        for it in itineraries:
            segs = it.get("segments") or []
            if segs:
                seg0 = segs[0]
                last_seg = segs[-1]
        flight_class = None
        for tp in (off.get("travelerPricings") or []):
            fds = (tp.get("fareDetailsBySegment") or [])
            if fds:
                flight_class = fds[0].get("segment", {}).get("cabin")
                break
        price = (off.get("price") or {}).get("total")
        try:
            price_f = float(price) if price is not None else None
        except (TypeError, ValueError):
            price_f = None
        return {
            "id": str(off.get("id")),
            "flight_id": str(off.get("id")),
            "origin": (_city_name((seg0 or {}).get("departure", {}).get("iataCode"))
                       or (seg0 or {}).get("departure", {}).get("iataCode")),
            "destination": (_city_name((last_seg or {}).get("arrival", {}).get("iataCode"))
                            or (last_seg or {}).get("arrival", {}).get("iataCode")),
            "date": ((seg0 or {}).get("departure", {}).get("at") or "")[:10],
            "time": ((seg0 or {}).get("departure", {}).get("at") or "")[11:16],
            "price": price_f,
            "class": (flight_class or "economy").lower(),
            "airline": (off.get("validatingAirlineCodes") or ["?"])[0],
            "seats": len(off.get("travelerPricings") or []),
            "provider": self.name,
        }

    # -- book ---------------------------------------------------------------
    async def book_flight(self, flight_id: str, passengers: int = 1,
                          cabin_class: Optional[str] = None) -> Dict[str, Any]:
        offer = self._offers.get(str(flight_id)) if self._offers else None
        if not offer:
            return {"ok": False, "error": "amadeus_offer_expired",
                    "flight_id": flight_id, "provider": self.name}
        travelers = []
        count = max(1, int(passengers or 1))
        for i in range(count):
            idx = i + 1
            travelers.append({
                "id": str(idx),
                "dateOfBirth": "1986-04-12" if idx % 2 == 0 else "1982-01-16",
                "name": {"firstName": f"AERUPT{idx:02d}", "lastName": "TRAVELER"},
                "gender": "UNSPECIFIED",
                "contact": {
                    "emailAddress": f"aerupt.traveler{idx}@example.com",
                    "phones": [{"deviceType": "MOBILE",
                                "countryCallingCode": "33", "number": "6000000{0:02d}".format(idx)}],
                },
            })
        body = {"type": "flight-order", "flightOffers": [offer],
                "travelers": travelers}
        out = await self._post("/v1/booking/flight-orders", body)
        if not out.get("ok"):
            return out
        data = out.get("data") or {}
        if isinstance(data, list):
            data = data[0] if data else {}
        order_id = str(data.get("id") or "")
        self._orders[order_id] = {"flight_id": flight_id, "offer": offer}
        return {
            "ok": True,
            "booking": {"flight_id": flight_id, "reference": order_id},
            "flight_id": flight_id, "reference": order_id,
            "provider": self.name,
        }

    # -- cancel -------------------------------------------------------------
    async def cancel_booking(self, flight_id: Optional[str] = None,
                             reference: Optional[str] = None) -> Dict[str, Any]:
        order_id = None
        if reference:
            order_id = str(reference)
        elif flight_id:
            for oid, meta in (self._orders or {}).items():
                if meta.get("flight_id") == str(flight_id):
                    order_id = oid
                    break
        if not order_id:
            return {"ok": False, "error": "amadeus_booking_not_found",
                    "flight_id": flight_id, "reference": reference,
                    "provider": self.name}
        out = await self._delete_order(order_id)
        if not out.get("ok"):
            return out
        data = out.get("data") or {}
        if isinstance(data, list):
            data = data[0] if data else {}
        return {"ok": True,
                "cancelled": {"flight_id": flight_id, "reference": order_id},
                "flight_id": flight_id, "reference": order_id,
                "status": (data or {}).get("status"),
                "provider": self.name}

    async def _delete_order(self, order_id: str) -> Dict[str, Any]:
        tok = await self._token()
        if not tok:
            return {"ok": False, "error": "amadeus_not_authenticated"}
        async with await self._client() as client:
            resp = await client.delete(
                f"/v1/booking/flight-orders/{order_id}",
                headers={"Authorization": f"Bearer {tok}"})
        return self._dispatch(resp, f"/v1/booking/flight-orders/{order_id}")