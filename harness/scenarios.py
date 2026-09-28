"""Canonical evaluation scenarios (mirroring the public test suite).

The kit: 9 canonical scenarios, 50% text / 30% audio / 20% visual —
covering interruptions, chained calls, retries, clarifications, and unseen
tools. Each scenario is a scripted, timestamped event stream plus gold
expectations for the local scorer.
"""
from __future__ import annotations

import base64
from typing import Dict, List

from .mockenv import make_png, make_wav

FLIGHT_MANIFEST = {
    "tools": [
        {"name": "flight_search", "params": [
            {"name": "origin", "type": "string", "required": True},
            {"name": "destination", "type": "string", "required": True},
            {"name": "date", "type": "string"},
            {"name": "max_price", "type": "integer"},
        ]},
        {"name": "book_flight", "params": [
            {"name": "flight_id", "type": "string", "required": True},
            {"name": "passengers", "type": "integer"},
        ], "state_modifying": True},
        {"name": "cancel_booking", "params": [
            {"name": "flight_id", "type": "string", "required": True},
            {"name": "reference", "type": "string"},
        ], "state_modifying": True},
    ],
    "reference_date": "2026-09-24",
}

SUPPORT_MANIFEST = {
    "tools": [
        {"name": "create_ticket", "params": [
            {"name": "subject", "type": "string", "required": True},
            {"name": "description", "type": "string"},
        ], "state_modifying": True},
    ],
    "reference_date": "2026-09-24",
}

MANUAL_MANIFEST = {
    "tools": [
        {"name": "manual_lookup", "params": [
            {"name": "query", "type": "string", "required": True},
            {"name": "device_model", "type": "string",
             "enum": ["Washer", "Dryer", "Fridge", "TV"]},
            {"name": "frame", "type": "image"},
        ]},
    ],
    "reference_date": "2026-09-24",
}

UNSEEN_MANIFEST = {
    "tools": [
        {"name": "rent_car", "params": [
            {"name": "vehicle_type", "type": "string",
             "enum": ["Compact", "SUV", "Sedan", "Van"]},
            {"name": "location", "type": "string"},
            {"name": "days", "type": "integer"},
        ]},
    ],
    "reference_date": "2026-09-24",
}

def _t(ts, text, end=True, partial=False):
    return {"ts": ts, "type": "transcript",
            "data": {"text": text, "end_of_turn": bool(end), "partial": bool(partial)}}


def _int(ts, reason="user_override"):
    return {"ts": ts, "type": "interrupt", "data": {"reason": reason}}


def _audio(ts):
    return {"ts": ts, "type": "audio",
            "data": {"wav": base64.b64encode(make_wav(seconds=1.3)).decode()}}


def _frame(ts):
    return {"ts": ts, "type": "frame",
            "data": {"png": base64.b64encode(make_png(320, 240)).decode()}}


SM = ("flight_search", "book_flight")

SCENARIOS: List[Dict] = [
    {
        "name": "s01_simple_chain_booking",
        "multimodal": False,
        "manifest": FLIGHT_MANIFEST,
        "events": [
            _t(0.0, "Book a flight from Paris to", end=False),
            _t(0.30, "Book a flight from Paris to London tomorrow.", end=True),
        ],
        "gold": {
            "tools": list(SM), "chain": True,
            "state_modifying_tools": ["book_flight"],
            "final_intent": "book_flight",
            "final_slots": {"origin": "paris", "destination": "london"},
            "response_must_contain": ["SU450", "booked"],
            "latency_ms": 800,
        },
    },
    {
        "name": "s02_interrupt_retarget_destination",
        "multimodal": True,
        "manifest": FLIGHT_MANIFEST,
        "events": [
            _t(0.0, "Search for flights from Paris to London tomorrow.", end=True),
            _int(0.65),
            _t(0.72, "Actually, make that Tokyo.", end=True),
        ],
        "gold": {
            "tools": ["flight_search"],
            "final_intent": "flight_search",
            "final_slots": {"destination": "tokyo"},
            "response_must_contain": ["Tokyo", "SU453"],
            "must_cancel": True,
            "latency_ms": 800,
            "multimodal": True,
            "hidden_15x": True,
        },
    },
    {
        "name": "s03_interrupt_mid_booking_adjust",
        "multimodal": False,
        "manifest": FLIGHT_MANIFEST,
        "events": [
            _t(0.0, "Book a flight from Paris to London tomorrow.", end=True),
            _int(0.6),
            _t(0.68, "Make it two passengers, business class instead.", end=True),
        ],
        "gold": {
            "tools": list(SM), "chain": True,
            "state_modifying_tools": ["book_flight"],
            "final_intent": "book_flight",
            "final_slots": {"passengers": 2, "class": "business"},
            "response_must_contain": ["SU452", "booked"],
            "must_cancel": True,
            "latency_ms": 800,
        },
    },
    {
        "name": "s04_speech_repair_disfluency",
        "multimodal": False,
        "manifest": FLIGHT_MANIFEST,
        "events": [
            _t(0.0, "Book a flight from Paris to… hmm sorry, "
                    "I mean from Berlin to London tomorrow.", end=True),
        ],
        "gold": {
            "tools": list(SM), "chain": True,
            "state_modifying_tools": ["book_flight"],
            "final_intent": "book_flight",
            "final_slots": {"origin": "berlin", "destination": "london"},
            "response_must_contain": ["SU460", "booked"],
            "latency_ms": 800,
        },
    },
    {
        "name": "s05_chained_clarify_fill",
        "multimodal": False,
        "manifest": FLIGHT_MANIFEST,
        "events": [
            _t(0.0, "Book a flight to London tomorrow.", end=True),
            _t(0.9, "From Berlin.", end=True),
        ],
        "gold": {
            "tools": list(SM), "chain": True,
            "state_modifying_tools": ["book_flight"],
            "final_intent": "book_flight",
            "final_slots": {"origin": "berlin", "destination": "london"},
            "response_must_contain": ["SU460", "booked"],
            "expect_clarify": True,
            "latency_ms": 800,
        },
    },
    {
        "name": "s06_retry_fault_injection",
        "multimodal": False,
        "seed": 5,
        "manifest": FLIGHT_MANIFEST,
        "events": [
            _t(0.0, "Search for flights from Paris to Tokyo tomorrow.", end=True),
        ],
        "gold": {
            "tools": ["flight_search"],
            "final_intent": "flight_search",
            "final_slots": {"destination": "tokyo"},
            "response_must_contain": ["SU453", "flights"],
            "fail_first_search": True,
            "latency_ms": 700,
        },
    },
    {
        "name": "s07_unseen_tool_rent_car",
        "multimodal": False,
        "manifest": UNSEEN_MANIFEST,
        "events": [
            _t(0.0, "I need to rent an SUV in Barcelona for 3 days.", end=True),
        ],
        "gold": {
            "tools": ["rent_car"],
            "final_intent": "rent_car",
            "final_slots": {"vehicle_type": "suv"},
            "response_must_contain": ["suv"],
            "latency_ms": 600,
            "hidden_15x": True,
        },
    },
    {
        "name": "s08_visual_frame_grounded_manual",
        "multimodal": True,
        "manifest": MANUAL_MANIFEST,
        "events": [
            _frame(0.0),
            _t(0.6, "Why won't my washer spin? Check the manual.", end=True),
        ],
        "gold": {
            "tools": ["manual_lookup"],
            "final_intent": "manual_lookup",
            "final_slots": {"device_model": "Washer"},
            "response_must_contain": ["manual"],
            "must_cancel": False,
            "latency_ms": 700,
            "multimodal": True,
            "hidden_15x": True,
        },
    },
    {
        "name": "s09_audio_first_then_action",
        "multimodal": True,
        "manifest": FLIGHT_MANIFEST,
        "events": [
            _audio(0.0),
            _t(0.5, "Book a flight from New York to London tomorrow.", end=True),
        ],
        "gold": {
            "tools": list(SM), "chain": True,
            "state_modifying_tools": ["book_flight"],
            "final_intent": "book_flight",
            "final_slots": {"origin": "new york", "destination": "london"},
            "response_must_contain": ["SU470", "booked"],
            "latency_ms": 800,
            "multimodal": True,
            "hidden_15x": True,
        },
    },
    {
        "name": "s10_session_slot_correction",
        "multimodal": False,
        "manifest": FLIGHT_MANIFEST,
        "events": [
            _t(0.0, "Search for flights from Paris to London tomorrow.", end=True),
            _t(0.35, "Actually to Tokyo.", end=True),
        ],
        "gold": {
            "tools": ["flight_search"],
            "final_intent": "flight_search",
            "final_slots": {"destination": "tokyo"},
            "response_must_contain": ["Tokyo", "SU453"],
            "must_cancel": True,
            "latency_ms": 800,
        },
    },
    {
        "name": "s11_support_ticket",
        "multimodal": False,
        "manifest": SUPPORT_MANIFEST,
        "events": [
            _t(0.0, "Please create a support ticket about my fridge not cooling properly.", end=True),
        ],
        "gold": {
            "tools": ["create_ticket"],
            "state_modifying_tools": ["create_ticket"],
            "final_intent": "create_ticket",
            "final_slots": {"subject": "my fridge not cooling properly"},
            "response_must_contain": ["ticket"],
            "latency_ms": 600,
        },
    },
    {
        "name": "s12_cancel_booking",
        "multimodal": False,
        "manifest": FLIGHT_MANIFEST,
        "events": [
            _t(0.0, "Book a flight from Paris to London tomorrow.", end=True),
            _t(3.0, "Actually, cancel my booking for flight SU450.", end=True),
        ],
        "gold": {
            "tools": ["flight_search", "book_flight", "cancel_booking"],
            "state_modifying_tools": ["book_flight", "cancel_booking"],
            "final_intent": "cancel_booking",
            "final_slots": {"flight_ids": ["SU450"]},
            "response_must_contain": ["SU450", "cancelled"],
            "latency_ms": 800,
        },
    },
    {
        "name": "s13_rapid_multi_correction_storm",
        "multimodal": False,
        "manifest": FLIGHT_MANIFEST,
        "events": [
            _t(0.0, "Book a flight from Paris to London tomorrow.", end=True),
            _int(0.5),
            _t(0.55, "Actually, make that to Tokyo instead.", end=True),
            _int(0.8),
            _t(0.85, "Make it business class.", end=True),
            _int(1.1),
            _t(1.15, "Just one passenger.", end=True),
        ],
        "gold": {
            "tools": ["flight_search", "book_flight"],
            "state_modifying_tools": ["book_flight"],
            "final_intent": "book_flight",
            "final_slots": {"destination": "tokyo", "class": "business",
                            "passengers": 1},
            "response_must_contain": ["SU454", "booked"],
            "must_cancel": True,
            "latency_ms": 800,
        },
    },
]