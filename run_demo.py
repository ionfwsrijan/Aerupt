"""Interactive console demo of aerupt.

    py run_demo.py
    py run_demo.py --scale 0.3 --vol '/path/to/recording.wav'

Type lines; AERUPT narrates/asks/acts back through the same terminal.
Behaviors to try:
  * "Book a flight from Paris to London tomorrow."
  * Interrupt: "Actually to Tokyo, make it two passengers."
  * Repair: "Search from Berlin to … sorry, I mean from New York to LA."
  * Retry: search flight, then "Book it."
"""
from __future__ import annotations

import argparse
import asyncio
import sys

sys.path.insert(0, ".")

from aerupt.agent import InterruptibleAgent
from aerupt.config import load_env
from aerupt.protocol import Event
from aerupt.utils import Clock, canonical_json
from harness.mockenv import MockEnv
from harness.scenarios import FLIGHT_MANIFEST

load_env()


async def _main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scale", type=float, default=0.3, help="sim time scale")
    args = ap.parse_args()

    clock = Clock(scale=args.scale)
    agent = InterruptibleAgent()
    env = MockEnv(clock=clock, latency_ms=650)
    channel = asyncio.Queue()
    env_events = asyncio.Queue()

    # agent -> world (and console): tool calls resolve in MockEnv, other
    # events narrate the assistant's state.
    async def emit_env(d) -> None:
        env_events.put_nowait(d)

    async def pump() -> None:
        while True:
            ev = await channel.get()
            data = ev.get("data", {})
            print(f"  [AERUPT {ev['type']}] {canonical_json(data)}", flush=True)
            if ev["type"] == "tool_call":
                asyncio.create_task(env.on_tool_call(ev, emit_env))
            elif ev["type"] == "cancel":
                asyncio.create_task(env.on_cancel(ev, emit_env))

    # env -> agent: resolutions are replayed back into the pipeline.
    async def env_feed() -> None:
        while True:
            ev = await env_events.get()
            await agent.handle_event(Event.from_dict(ev))

    pump_task = asyncio.create_task(pump())
    env_task = asyncio.create_task(env_feed())

    async def route_out(action_dict) -> None:
        channel.put_nowait(action_dict)

    agent.orch.clock = clock
    agent.orch.brain._ref = None
    agent.orch._send = route_out
    await agent.setup()
    await agent.handle_event(
        Event("manifest", FLIGHT_MANIFEST, ts=0.0)
    )

    print("AERUPT demo — type your request, then watch AERUPT react (Ctrl+C to quit).")
    while True:
        raw = input("\n  you> ").strip()
        if not raw:
            continue
        if raw in ("/quit", "/exit", "quit", "exit"):
            print("  [AERUPT] Bye!")
            break
        await agent.handle_event(
            Event("transcript", {"text": raw, "end_of_turn": True},
                  ts=clock.now())
        )

    pump_task.cancel()
    env_task.cancel()
    agent.stop()


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        print("\nbye")