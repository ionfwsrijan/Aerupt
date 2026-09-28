"""Virtual-clock streaming harness.

Replays a scenario's timestamped events at the correct points on a shared,
injectable Clock, feeds the agent's emitted actions to a MockEnv ('external'
tool executor), and records every event/action/result in a full trace log —
the same mechanics the evaluation kit uses.
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Dict, List, Optional

from aerupt.agent import InterruptibleAgent
from aerupt.protocol import Event
from aerupt.utils import Clock
from .mockenv import MockEnv

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "runs")


class ScenarioRunner:
    """Wires agent + mockenv + virtual clock; replays one scenario."""

    def __init__(self, agent: InterruptibleAgent, env: MockEnv,
                 clock: Optional[Clock] = None,
                 settle_ms: float = 3800.0,
                 time_limit_s: float = 60.0):
        self.agent = agent
        self.env = env
        self.clock = clock or Clock(scale=1.0)
        self.settle_ms = settle_ms
        self.time_limit_s = time_limit_s
        self.idle_timeout_s = 12.0
        self.trace: List[Dict[str, Any]] = []
        self._pump_task: Optional[asyncio.Task] = None
        self._failed_first = False

    # ------------------------------------------------------------------
    async def run(self, scenario: Dict[str, Any]) -> Dict[str, Any]:
        events = scenario["events"]
        scale = max(self.clock._scale, 1e-6)

        # event channel from the mockenv back into the agent
        channel: asyncio.Queue = asyncio.Queue()
        self._pump_task = asyncio.create_task(self._pump(channel, scale))
        self.trace.clear()

        async def emit_from_env(event_dict: Dict[str, Any]) -> None:
            await channel.put(event_dict)

        async def send(action_dict: Dict[str, Any]) -> None:
            self.trace.append(dict(action_dict))
            kind = action_dict.get("type")
            if kind == "tool_call":
                d = action_dict["data"]
                if scenario.get("gold", {}).get("fail_first_search") \
                        and "search" in d["tool"] and not self._failed_first:
                    self.env.inject_fault(d["call_id"])
                    self._failed_first = True
                asyncio.create_task(self.env.on_tool_call(action_dict, emit_from_env))
            elif kind == "cancel":
                asyncio.create_task(self.env.on_cancel(action_dict, emit_from_env))

        # rebind the agent's emitter to our pipeline
        self.agent.orch._send = send
        self.agent.orch.clock = self.clock
        self.agent.orch.brain._ref = None

        manifest = scenario.get("manifest")
        if manifest:
            await self.agent.handle_event(Event("manifest", manifest, ts=0.0))

        deadline = self._mono() + self.time_limit_s
        prev = 0.0
        # Pacing: clock.sleep(dt) already applies `*scale`, so scenario deltas
        # are passed UNSCALED. This keeps events and tool latencies advancing
        # by the same factor, so relative timing is invariant to --scale.
        for ev in events:
            if self._mono() > deadline:
                break
            ts = float(ev.get("ts", prev))
            if ts - prev > 0:
                await self.clock.sleep(ts - prev)
            await self.agent.handle_event(Event.from_dict(ev))
            prev = ts

        await self.clock.sleep(self.settle_ms / 1000.0)

        # Idle barrier: keep the run alive while the env still has in-flight
        # tool calls, so the final epoch always consumes its last result and
        # emits its response before the harness tears everything down.
        idle_deadline = self._mono() + self.idle_timeout_s
        while self.env.active > 0 and self._mono() < idle_deadline:
            await self.clock.sleep(0.02)
        await self.agent.handle_event(Event("end", {}, ts=self.clock.now()))
        await self.agent.flush()

        if self._pump_task:
            self._pump_task.cancel()
            try:
                await self._pump_task
            except asyncio.CancelledError:
                pass
        # let any straggler mock tasks finish or be cancelled
        pending = [t for t in asyncio.all_tasks()
                   if t.get_name().startswith("mock:")]
        for t in pending:
            t.cancel()

        return {
            "scenario": scenario.get("name"),
            "gold": scenario.get("gold", {}),
            "events": scenario.get("events", []),
            "trace": self.trace,            # actions emitted by the agent
            "agent_events": [],             # populated by scorer via pkg
            "env": self.env.summary(),
        }

    async def _pump(self, channel: asyncio.Queue, scale: float) -> None:
        while True:
            ev = await channel.get()
            await self.agent.handle_event(Event.from_dict(ev))

    def save(self, name: str, data: Dict[str, Any]) -> str:
        os.makedirs(OUT_DIR, exist_ok=True)
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
        path = os.path.join(OUT_DIR, f"{safe}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, default=str)
        return path

    @staticmethod
    def _mono() -> float:
        import time
        return time.monotonic()


async def run_suite(agent_factory, scenarios: List[Dict[str, Any]],
                    scale: float = 1.0, settle_ms: float = 3800.0,
                    save: bool = True) -> List[Dict[str, Any]]:
    results = []
    for scenario in scenarios:
        clock = Clock(scale=scale)
        agent = agent_factory()
        env = MockEnv(clock=clock,
                      latency_ms=scenario.get("gold", {}).get("latency_ms", 700),
                      fail_rate=scenario.get("gold", {}).get("fail_rate", 0.0),
                      rng_seed=scenario.get("seed", 11))
        runner = ScenarioRunner(agent, env, clock=clock, settle_ms=settle_ms)
        res = await runner.run(scenario)
        if save:
            res["trace_path"] = runner.save(res["scenario"], res)
        results.append(res)
    return results