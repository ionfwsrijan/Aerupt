"""Public agent facade.

`InterruptibleAgent` speaks over two asynchronous queues exactly as the
theme's interface contract states: timestamped events in, actions out.

    queue_in  : producer feeds scenario events (transcript, audio, frame,
                interrupt, tool_result, manifest, start_of_turn, end)
    queue_out : consumer reads the agent's actions (filler, narration,
                tool_call, cancel, clarify, response)

`run()` processes the input queue until an `end` event. `ainput()` lets a
harness push events directly; emitted actions are sent to `queue_out` unless
a custom `send` is provided.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

from .config import Config
from .orchestrator import Orchestrator
from .protocol import EVENT_END, Event

log = logging.getLogger("AERUPT")


class InterruptibleAgent:
    def __init__(self, config: Optional[Dict[str, Any]] = None,
                 send: Optional[Any] = None):
        self.config = Config.from_dict(config or {})
        self.queue_in: asyncio.Queue = asyncio.Queue()
        self.queue_out: asyncio.Queue = asyncio.Queue()

        async def default_send(action_dict: Dict[str, Any]) -> None:
            await self.queue_out.put(action_dict)

        self.orch = Orchestrator(self.config, send=send or default_send)
        self._pump_task: Optional[asyncio.Task] = None

    # -- interface ------------------------------------------------------------
    def submit(self, event: Any) -> "InterruptibleAgent":
        """Push an event (dict or Event) from any thread/locale."""
        if isinstance(event, Event):
            ev = event
        else:
            ev = Event.from_dict(event)
        self.queue_in.put_nowait(ev)
        return self

    async def ainput(self, event: Any) -> None:
        await self.queue_in.put(event if isinstance(event, Event)
                                else Event.from_dict(event))

    async def run(self, input_queue: Optional[asyncio.Queue] = None,
                  output_queue: Optional[asyncio.Queue] = None) -> None:
        """Process events until an `end` event. Optionally bind external
        queues (timestamped events in / actions out)."""
        qin = input_queue or self.queue_in
        if output_queue is not None:
            self.queue_out = output_queue

            async def bound_send(action_dict: Dict[str, Any]) -> None:
                await output_queue.put(action_dict)

            self.orch._send = bound_send

        while True:
            ev = await qin.get()
            await self.orch.handle(ev)
            if ev.type == EVENT_END:
                break
        await self.flush()

    async def flush(self) -> None:
        # drain any stray completion work before returning
        tasks = [t for t in self.orch.tasks if not t.done()]
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=0.05)
            for t in pending:
                t.cancel()

    # -- lifecycle --------------------------------------------------------------
    async def setup(self, ctx: Optional[Dict[str, Any]] = None) -> None:
        """Warm-up / manifest hook (allowed 300s in the evaluation kit)."""
        manifest = (ctx or {}).get("manifest")
        if manifest:
            await self.handle_event(Event("manifest", manifest))
            await self._pump(1)

    async def _pump(self, n: int) -> None:
        for _ in range(n):
            if self.queue_in.empty():
                break
            ev = self.queue_in.get_nowait()
            await self.orch.handle(ev)

    @property
    def snapshot(self) -> Dict[str, Any]:
        return self.orch.snapshot.to_full_dict()

    @property
    def trace(self) -> list:
        return self.orch.trace

    # -- helpers for harnesses ----------------------------------------------------
    async def handle_event(self, ev: Any) -> None:
        await self.orch.handle(ev if isinstance(ev, Event) else Event.from_dict(ev))

    def stop(self) -> None:
        self.orch._end(Event(EVENT_END, {}, 0.0))