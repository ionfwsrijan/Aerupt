"""Unit tests: NLU extraction, ledger idempotency, planner chains, and an
end-to-end scenario replay. Run with:  py -m unittest discover -s tests"""
import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aerupt.nlu import NLU
from aerupt.tools import IdempotencyLedger, ToolRegistry
from aerupt.utils import fingerprint


def _mk_agent():
    from aerupt.agent import InterruptibleAgent
    return InterruptibleAgent()


class TestNLU(unittest.TestCase):
    def setUp(self):
        self.n = NLU()
        self.n.load_manifest([
            {"name": "flight_search", "params": [
                {"name": "origin"}, {"name": "destination"}, {"name": "date"}]},
            {"name": "book_flight", "params": [
                {"name": "flight_id", "required": True}]},
        ])

    def test_from_to(self):
        r = self.n.parse_turn("Book a flight from Paris to London tomorrow.")
        self.assertEqual(r["intent"], "book_flight")
        self.assertEqual(r["slots"]["origin"], "paris")
        self.assertEqual(r["slots"]["destination"], "london")

    def test_multiword_city(self):
        r = self.n.parse_turn("Search for flights from New York to Los Angeles.")
        self.assertEqual(r["slots"]["origin"], "new york")
        self.assertEqual(r["slots"]["destination"], "los angeles")

    def test_repair_override(self):
        r = self.n.parse_turn("Book a flight from Paris to… hmm sorry, "
                              "I mean from Berlin to London tomorrow.")
        self.assertTrue(r["corrected"])
        self.assertEqual(r["slots"]["origin"], "berlin")
        self.assertEqual(r["slots"]["destination"], "london")

    def test_no_false_count(self):
        s = NLU.extract_slots(self.n, "Please create a support ticket about "
                                      "my fridge not cooling properly.")
        self.assertNotIn("passengers", s)


class TestLedger(unittest.TestCase):
    def test_dedupe(self):
        led = IdempotencyLedger()
        tool, args = "book_flight", {"flight_id": "SU450"}
        fp = fingerprint(tool, args)
        led.mark_issued(tool, args, "c1", fp)
        led.mark_completed(tool, args, {"ref": "X1"}, fp)
        dec = led.resolve_issue(tool, args)
        self.assertEqual(dec["kind"], "done")
        dec2 = led.resolve_issue(tool, {"flight_id": "SU451"})
        self.assertEqual(dec2["kind"], "new",
                          "different args must not dedupe")


class TestPlannerChain(unittest.TestCase):
    def test_book_implies_search(self):
        from aerupt.planner import Planner
        from aerupt.snapshot import StateSnapshot

        payload = {
            "tools": [
                {"name": "flight_search", "params": [
                    {"name": "origin", "required": True},
                    {"name": "destination", "required": True}]},
                {"name": "book_flight", "params": [
                    {"name": "flight_id", "required": True}],
                 "state_modifying": True},
            ]}
        reg = ToolRegistry()
        reg.load_manifest(payload)
        n = NLU()
        n.load_manifest(payload["tools"])
        snap = StateSnapshot()
        snap.intent = "book_flight"
        snap.slots = {"origin": "paris", "destination": "london"}
        parse = {"intent": "book_flight",
                 "slots": {"origin": "paris", "destination": "london"}}
        plan = Planner(n, reg).build("Book a flight from Paris to London.",
                                     parse, snap, None)
        tools = [st.tool for st in plan.steps]
        self.assertEqual(tools, ["flight_search", "book_flight"])
        self.assertEqual(plan.steps[-1].deps, [0])


class TestEndToEndScenario(unittest.TestCase):
    def _run_scenario(self, name, scale=0.4):
        from harness.harness import ScenarioRunner
        from harness.mockenv import MockEnv
        from harness.scenarios import SCENARIOS
        from harness.scorer import Scorer
        from aerupt.utils import Clock
        from aerupt.agent import InterruptibleAgent

        async def run():
            s = next(sc for sc in SCENARIOS if sc["name"] == name)
            clock = Clock(scale=scale)
            agent = InterruptibleAgent(config={"time_scale": scale})
            env = MockEnv(clock=clock, latency_ms=800)
            res = await ScenarioRunner(agent, env, clock=clock,
                                       settle_ms=3500).run(s)
            return Scorer(res["trace"], res["gold"], res["env"],
                          res["events"]).score()

        return asyncio.run(run())[0]

    def test_s01(self):
        from harness.harness import ScenarioRunner
        from harness.mockenv import MockEnv
        from harness.scenarios import SCENARIOS
        from harness.scorer import Scorer
        from aerupt.utils import Clock

        async def run():
            s = SCENARIOS[0]
            clock = Clock(scale=0.2)
            agent = _mk_agent()
            env = MockEnv(clock=clock, latency_ms=800)
            res = await ScenarioRunner(agent, env, clock=clock,
                                       settle_ms=3500).run(s)
            score, _ = Scorer(res["trace"], res["gold"], res["env"],
                              res["events"]).score()
            return score, res["env"].get("bookings", [])

        score, bookings = asyncio.run(run())
        self.assertEqual(score, 100.0)
        self.assertTrue(bookings, "expected a completed booking")

    def test_s12_cancel(self):
        score = self._run_scenario("s12_cancel_booking")
        self.assertEqual(score, 100.0)

    def test_s13_rapid_corrections(self):
        score = self._run_scenario("s13_rapid_multi_correction_storm")
        self.assertEqual(score, 100.0)


class TestScorerDetail(unittest.TestCase):
    def test_checks_report_is_populated(self):
        from harness.harness import ScenarioRunner
        from harness.mockenv import MockEnv
        from harness.scenarios import SCENARIOS
        from harness.scorer import Scorer
        from aerupt.utils import Clock
        from aerupt.agent import InterruptibleAgent

        async def run():
            s = SCENARIOS[0]
            clock = Clock(scale=0.4)
            agent = InterruptibleAgent(config={"time_scale": 0.4})
            env = MockEnv(clock=clock, latency_ms=800)
            res = await ScenarioRunner(agent, env, clock=clock,
                                       settle_ms=3500).run(s)
            _, detail = Scorer(res["trace"], res["gold"], res["env"],
                               res["events"]).score()
            return detail

        detail = asyncio.run(run())
        self.assertTrue(detail.get("checks_report"),
                        "score() must attach a populated checks_report")
        self.assertTrue(any(c["check"].startswith("task:grounding")
                            for c in detail["checks_report"]))


class TestMultiTurnSession(unittest.TestCase):
    """A live session must not let one turn's booking bleed into the next:
    "book tokyo" after a london booking must re-pick from a fresh search."""

    def _drive(self, texts, scale=0.4):
        from aerupt.agent import InterruptibleAgent
        from aerupt.protocol import Event
        from aerupt.utils import Clock
        from harness.mockenv import MockEnv
        from harness.scenarios import FLIGHT_MANIFEST

        async def run():
            clock = Clock(scale=scale)
            agent = InterruptibleAgent()
            env = MockEnv(clock=clock, latency_ms=650)
            channel = asyncio.Queue()
            env_events = asyncio.Queue()
            responses: list = []
            book_calls: list = []

            async def route_out(action):
                channel.put_nowait(action)
                if action["type"] == "tool_call" \
                        and action["data"].get("tool") == "book_flight":
                    book_calls.append(dict(action["data"]["args"]))
                elif action["type"] == "response":
                    responses.append(action["data"].get("text", ""))

            async def emit(d):
                env_events.put_nowait(d)

            async def pump():
                while True:
                    act = await channel.get()
                    if act["type"] == "tool_call":
                        asyncio.create_task(env.on_tool_call(act, emit))
                    elif act["type"] == "cancel":
                        asyncio.create_task(env.on_cancel(act, emit))

            async def env_feed():
                while True:
                    ev = await env_events.get()
                    await agent.handle_event(Event.from_dict(ev))

            agent.orch.clock = clock
            agent.orch.brain._ref = None
            agent.orch._send = route_out
            agent.orch.begin_scenario("2026-09-23")
            await agent.setup()
            await agent.handle_event(
                Event("manifest", FLIGHT_MANIFEST, ts=0.0))
            asyncio.create_task(pump())
            asyncio.create_task(env_feed())
            for idx, t in enumerate(texts):
                await agent.handle_event(
                    Event("transcript", {"text": t, "end_of_turn": True},
                          ts=clock.now()))
                await asyncio.sleep(0.2)
                for _ in range(400):
                    if len(responses) >= idx + 1:
                        break
                    await asyncio.sleep(0.05)

            agent.stop()
            return list(responses), list(book_calls)

        return asyncio.run(run())

    def test_second_turn_re_picks_from_fresh_search(self):
        responses, book_calls = self._drive([
            "Book a flight from Paris to London tomorrow.",
            "Book a business flight from Paris to Tokyo.",
        ])
        self.assertIn("SU450", responses[0])
        self.assertIn("SU454", responses[1],
                      "tokyo turn must book the business flight, not reuse SU450")
        self.assertEqual(book_calls[-1].get("flight_id"), "SU454")
        # distinct bookings — no silent reuse of the london reference
        self.assertNotEqual(responses[0], responses[1])


if __name__ == "__main__":
    unittest.main()