"""Tests for the aviation LLM + RAG layer and the flight-platform providers."""
from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from aerupt import llm, rag, responder, __version__
from aerupt.nlu import NLU
from aerupt.planner import Planner
from aerupt.providers import MockBookingProvider, active_provider, city_to_code, reset_provider_cache
from aerupt.providers.amadeus import AmadeusProvider
from aerupt.tools import ToolRegistry

KNOWLEDGE_SPEC = {
    "name": "kb_lookup",
    "params": [
        {"name": "query", "type": "string", "required": True},
        {"name": "topic", "type": "string",
         "enum": ["baggage", "security", "refunds", "airports"]},
    ],
}
UI_TOOLS = [
    {"name": "flight_search", "params": [
        {"name": "origin", "type": "string", "required": True},
        {"name": "destination", "type": "string", "required": True},
        {"name": "date", "type": "string"}]},
    {"name": "book_flight", "params": [
        {"name": "flight_id", "type": "string", "required": True},
        {"name": "passengers", "type": "integer"}],
        "state_modifying": True},
    {"name": "cancel_booking", "params": [
        {"name": "flight_id", "type": "string", "required": True}],
        "state_modifying": True},
    KNOWLEDGE_SPEC,
]

OFFER = {
    "id": "OFFER987",
    "itineraries": [{"segments": [
        {"departure": {"iataCode": "CDG", "at": "2026-10-01T08:30:00"},
         "arrival": {"iataCode": "LHR", "at": "2026-10-01T09:05:00"}}]}],
    "price": {"total": "712.50", "currency": "EUR"},
    "travelerPricings": [{"fareDetailsBySegment": [
        {"segment": {"cabin": "BUSINESS"}}]}],
    "validatingAirlineCodes": ["SU"],
}


class TestRAG(unittest.TestCase):
    def setUp(self):
        self.engine = rag.RAGEngine()

    def test_corpus_loads(self):
        self.assertGreater(len(self.engine), 30, "knowledge corpus too small")

    def test_retrieval_grounds(self):
        hits = self.engine.search("baggage carry-on size limit", 3)
        self.assertTrue(hits)
        self.assertTrue(any("30_baggage" in h["source"] for h in hits))

    def test_answer_knowledge(self):
        out = asyncio.run(responder.answer_knowledge(
            "How many liquids can I take on board?", k=3))
        self.assertTrue(out["answer"])
        self.assertTrue(out["sources"])
        self.assertTrue(any("security" in s["source"] for s in out["sources"]))

    def test_kb_dir_env_override(self):
        with tempfile.TemporaryDirectory() as td:
            Path(td, "custom_kb.md").write_text(
                "# Widgets\nWidgets fly at 10 000 meters.\n", encoding="utf-8")
            os.environ["AERUPT_KB_DIR"] = td
            try:
                engine = rag.RAGEngine()
                self.assertEqual(len(engine), 1)
                hits = engine.search("widgets fly meters", 1)
                self.assertTrue(hits)
                self.assertEqual(hits[0]["source"], "custom_kb.md")
            finally:
                os.environ.pop("AERUPT_KB_DIR", None)

    def test_top_k_env_default(self):
        os.environ.pop("AERUPT_KB_TOP_K", None)
        self.assertEqual(rag._default_top_k(), 3)
        os.environ["AERUPT_KB_TOP_K"] = "1"
        try:
            self.assertEqual(rag._default_top_k(), 1)
        finally:
            os.environ.pop("AERUPT_KB_TOP_K", None)

    def test_kb_cli(self):
        with tempfile.TemporaryDirectory() as td:
            Path(td, "security.md").write_text(
                "# Liquids\nLiquids up to 100 ml per container.\n", encoding="utf-8")
            rc = rag.cli(["100 ml liquids", "--kb-dir", td, "--top-k", "1"])
            self.assertEqual(rc, 0)
            rc_miss = rag.cli(["zzqzx kwua", "--kb-dir", td, "--top-k", "1"])
            self.assertEqual(rc_miss, 1)

    def test_version_exposed(self):
        self.assertEqual(__version__, "1.1.0")


class TestLLMFallback(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in
                       ("GROQ_API_KEY", "AERUPT_LLM", "LLM_TIMEOUT_S")}

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        llm.load_llm_config.cache_clear() if hasattr(llm.load_llm_config, "cache_clear") else None

    def test_disabled_without_key(self):
        os.environ.pop("GROQ_API_KEY", None)
        os.environ.pop("AERUPT_LLM", None)
        self.assertFalse(llm.llm_available())
        self.assertIsNone(asyncio.run(llm.complete("sys", "hello")))

    def test_invalid_key_falls_back_to_snippet(self):
        os.environ["GROQ_API_KEY"] = "gsk_fake"
        os.environ["LLM_TIMEOUT_S"] = "2"
        os.environ["AERUPT_LLM"] = "1"
        out = asyncio.run(responder.answer_knowledge("can I bring a power bank?", k=3))
        self.assertTrue(out["answer"])
        self.assertTrue(out["sources"])


class TestKnowledgeIntent(unittest.TestCase):
    def test_detect_knowledge(self):
        nlu = NLU()
        nlu.load_manifest(UI_TOOLS)
        self.assertEqual(nlu.detect_intent("What is the carry-on limit?"), "kb_lookup")
        self.assertEqual(nlu.detect_intent("How many liquids can I bring?"), "kb_lookup")
        self.assertEqual(nlu.detect_intent("Am I allowed to bring a power bank?"), "kb_lookup")
        self.assertEqual(nlu.detect_intent("Book a flight from Paris to London."), "book_flight")
        self.assertEqual(nlu.detect_intent("Cancel the booking for SU450"), "cancel_booking")
        self.assertEqual(nlu.detect_intent("Search flights from Paris to Tokyo."), "flight_search")

    def test_planner_knowledge_step_keeps_raw_query(self):
        registry = ToolRegistry()
        registry.load_manifest([KNOWLEDGE_SPEC])
        nlu = NLU()
        nlu.load_manifest([KNOWLEDGE_SPEC])
        planner = Planner(nlu, registry)
        snapshot = SimpleNamespace(slots={})
        session = SimpleNamespace()
        parse = {"intent": "kb_lookup", "slots": {}}
        plan = planner.build("What is the carry-on limit?", parse, snapshot, session)
        self.assertEqual(len(plan.steps), 1)
        self.assertEqual(plan.steps[0].tool, "kb_lookup")
        self.assertEqual(plan.steps[0].build({}), {"query": "What is the carry-on limit?"})


class TestProviders(unittest.TestCase):
    def setUp(self):
        self._platform = os.environ.get("AERUPT_PLATFORM")
        reset_provider_cache()

    def tearDown(self):
        if self._platform is None:
            os.environ.pop("AERUPT_PLATFORM", None)
        else:
            os.environ["AERUPT_PLATFORM"] = self._platform
        reset_provider_cache()

    def test_city_codes(self):
        self.assertEqual(city_to_code("paris"), "PAR")
        self.assertEqual(city_to_code("new york"), "NYC")
        self.assertEqual(city_to_code("CDG"), "CDG")
        self.assertIsNone(city_to_code("atlantis"))

    def test_default_factory_mock(self):
        os.environ.pop("AERUPT_PLATFORM", None)
        self.assertEqual(active_provider().name, "mock")

    def test_amadeus_config_without_creds(self):
        os.environ["AERUPT_PLATFORM"] = "amadeus"
        prov = active_provider()
        self.assertEqual(prov.name, "amadeus")
        self.assertFalse(prov.configured)

    def test_mock_provider_roundtrip(self):
        prov = MockBookingProvider()
        book = asyncio.run(prov.book_flight("SU450", passengers=1))
        self.assertTrue(book["ok"])
        self.assertTrue(book["reference"].startswith("AER"))
        cancel = asyncio.run(prov.cancel_booking(reference=book["reference"]))
        self.assertTrue(cancel["ok"])

    def test_amadeus_normalization(self):
        from aerupt.providers.amadeus import AmadeusProvider
        prov = AmadeusProvider()
        out = prov._normalize_search({"data": [OFFER]})
        self.assertTrue(out["ok"])
        self.assertEqual(out["count"], 1)
        f = out["flights"][0]
        self.assertEqual(f["id"], "OFFER987")
        self.assertEqual(f["price"], 712.5)
        self.assertEqual(f["class"], "business")
        self.assertEqual(f["origin"], "Paris")
        self.assertEqual(f["destination"], "London")
        self.assertEqual(f["date"], "2026-10-01")

    def test_amadeus_offer_cache_populates(self):
        prov = AmadeusProvider()
        prov._normalize_search({"data": [OFFER]})
        self.assertIn("OFFER987", prov._offers)


class TestFlightStatus(unittest.TestCase):
    def setUp(self):
        from harness.scenarios import FLIGHT_MANIFEST_UI
        self.ui_tools = FLIGHT_MANIFEST_UI["tools"]
        self.names = [t["name"] for t in FLIGHT_MANIFEST_UI["tools"]]

    def test_manifest_ui_shares_flight_manifest(self):
        from harness.scenarios import FLIGHT_MANIFEST
        base = [t["name"] for t in FLIGHT_MANIFEST["tools"]]
        # The evaluated sweep manifest is unchanged (3 flight tools only).
        self.assertFalse(any("kb" in n or "status" in n for n in base))
        # The interactive manifest extends it with knowledge + status.
        for name in base:
            self.assertIn(name, self.names)
        self.assertIn("flight_status", self.names)
        self.assertIn("kb_lookup", self.names)

    def test_status_requires_flight_id(self):
        tool = next(t for t in self.ui_tools if t["name"] == "flight_status")
        self.assertTrue(any(p["name"] == "flight_id" and p["required"]
                            for p in tool["params"]))

    def test_intent_routes_status(self):
        from harness.scenarios import FLIGHT_MANIFEST_UI
        nlu = NLU()
        nlu.load_manifest(FLIGHT_MANIFEST_UI["tools"])
        self.assertEqual(nlu.detect_intent("Is my flight SU450 delayed?"),
                         "flight_status")
        self.assertEqual(nlu.detect_intent("What's the status of SU451?"),
                         "flight_status")
        self.assertEqual(nlu.detect_intent("Track my departure from Berlin"),
                         "flight_status")
        # Policy queries about delays must NOT become status lookups.
        self.assertEqual(nlu.detect_intent("baggage delay policy"),
                         "kb_lookup")
        self.assertEqual(nlu.detect_intent("how long is my delay"),
                         "kb_lookup")

    def test_env_status_is_deterministic(self):
        from aerupt.utils import Clock
        from harness.mockenv import MockEnv

        async def run():
            env = MockEnv(clock=Clock(scale=1.0))
            a = await env._run_tool("flight_status", {"flight_id": "SU450"})
            b = await env._run_tool("flight_status", {"flight_id": "SU450"})
            c = await env._run_tool("flight_status", {"flight_id": "SU451"})
            return a, b, c

        a, b, c = asyncio.run(run())
        self.assertTrue(a["ok"])
        self.assertEqual(a, b)
        self.assertEqual(a["flight_id"], "SU450")
        self.assertIn(a["status"], ["ON TIME", "DELAYED 40 MIN",
                                    "BOARDING", "AT GATE"])
        self.assertTrue(str(a["gate"]).startswith(("A", "B", "C", "D", "E")))
        self.assertIn(a["terminal"], (1, 2, 3))


class TestHealthEndpoint(unittest.TestCase):
    def setUp(self):
        for k in ("AERUPT_PLATFORM", "GROQ_API_KEY", "LLM_API_KEY",
                  "AERUPT_LLM"):
            os.environ.pop(k, None)
        from aerupt.providers import reset_provider_cache
        reset_provider_cache()

    def test_health_reports_stack(self):
        from ui.server.main import health
        out = asyncio.run(health())
        self.assertTrue(out["ok"])
        self.assertEqual(out["agent"], "AERUPT")
        self.assertIn("version", out)
        self.assertEqual(out["platform"], "mock")
        self.assertEqual(out["mode"], "deterministic")


if __name__ == "__main__":
    unittest.main()