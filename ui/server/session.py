"""Persistent AERUPT session bridged over a websocket.

One agent + mock tool environment + virtual clock per connection. Actions
stream out as JSON; transcripts, partials, interrupts and resets stream in.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

from aerupt.agent import InterruptibleAgent
from aerupt.protocol import Event
from aerupt.utils import Clock
from harness.mockenv import MockEnv
from harness.scenarios import FLIGHT_MANIFEST_UI

REFERENCE_TODAY = "2026-09-23"


class AgentSession:
    def __init__(self, outbox: asyncio.Queue, latency_ms: int = 650,
                 scale: float = 0.6, today: str = REFERENCE_TODAY):
        self.outbox = outbox
        self.latency_ms = latency_ms
        self.scale = scale
        self.today = today
        self.agent: Optional[InterruptibleAgent] = None
        self.env: Optional[MockEnv] = None
        self.clock: Optional[Clock] = None
        self._channel: Optional[asyncio.Queue] = None
        self._env_events: Optional[asyncio.Queue] = None
        self._tasks: list = []
        self._tool_names: dict = {}

    # -- lifecycle ---------------------------------------------------------
    async def start(self) -> None:
        self.clock = Clock(scale=self.scale)
        self.agent = InterruptibleAgent()
        self.env = MockEnv(clock=self.clock, latency_ms=self.latency_ms)
        self._channel = asyncio.Queue()
        self._env_events = asyncio.Queue()

        async def route_out(action: Dict[str, Any]) -> None:
            self._channel.put_nowait(action)
            await self.outbox.put({
                "type": "action",
                "kind": action.get("type"),
                "ts": round(float(action.get("ts", 0.0)), 3),
                "data": action.get("data", {}) or {},
            })

        async def emit_env(d: Dict[str, Any]) -> None:
            self._env_events.put_nowait(d)
            if d.get("type") == "tool_result":
                data = dict(d.get("data", {}) or {})
                cid = str(data.get("call_id") or "")
                data["tool"] = self._tool_names.get(cid, "tool")
                await self.outbox.put({
                    "type": "tool_result",
                    "kind": "tool_result",
                    "ts": round(float(d.get("ts", 0.0)), 3),
                    "data": data,
                })

        async def pump() -> None:
            while True:
                act = await self._channel.get()
                kind = act.get("type")
                if kind == "tool_call":
                    self._tool_names[act["data"].get("call_id")] = act["data"].get("tool")
                    asyncio.create_task(self.env.on_tool_call(act, emit_env))
                elif kind == "cancel":
                    asyncio.create_task(self.env.on_cancel(act, emit_env))

        async def env_feed() -> None:
            while True:
                ev = await self._env_events.get()
                await self.agent.handle_event(Event.from_dict(ev))

        self.agent.orch.clock = self.clock
        self.agent.orch.brain._ref = None
        self.agent.orch._send = route_out
        self.agent.orch.begin_scenario(self.today)
        await self.agent.setup()
        await self.agent.handle_event(Event("manifest", FLIGHT_MANIFEST_UI, ts=0.0))

        pump_task = asyncio.create_task(pump())
        env_task = asyncio.create_task(env_feed())
        self._tasks.extend([pump_task, env_task])

        await self.outbox.put({
            "type": "ready",
            "ts": 0.0,
            "manifest": [t["name"] for t in FLIGHT_MANIFEST_UI["tools"]],
        })

    async def stop(self) -> None:
        if self.agent is not None:
            self.agent.stop()
        for t in self._tasks:
            t.cancel()
        self._tasks.clear()

    # -- inbound -----------------------------------------------------------
    async def partial(self, text: str) -> None:
        if self.agent is None or not text:
            return
        await self.agent.handle_event(Event(
            "transcript", {"text": text, "end_of_turn": False, "partial": True},
            ts=self.clock.now()))

    async def user_text(self, text: str) -> None:
        if self.agent is None or not text:
            return
        await self.agent.handle_event(Event(
            "transcript", {"text": text, "end_of_turn": True},
            ts=self.clock.now()))

    async def interrupt(self, reason: str = "user_override") -> None:
        if self.agent is None:
            return
        await self.agent.handle_event(Event(
            "interrupt", {"reason": reason}, ts=self.clock.now()))