"""Grounded answering: RAG retrieval + Groq LLM composition, with a
deterministic fallback so the agent never depends on the network.

The answer flow for aviation-knowledge questions:
  1. retrieve top-k chunks from the aviation KB (`aerupt.rag`)
  2. assemble a strict grounding context (source + heading + text)
  3. if an LLM key is present, ask Groq to answer **only** from that context
     and cite its sources; otherwise compose a snippet from the best chunk.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from . import llm
from .rag import get_rag

SYSTEM_PROMPT = (
    "You are AERUPT, the voice assistant of an aviation booking platform. You answer "
    "travelers' questions about flights, airports, baggage, security rules, refunds, "
    "rebooking, and loyalty.\n"
    "Hard rules:\n"
    "1. Answer ONLY from the CONTEXT blocks below. Never invent facts, flights, "
    "prices, policies, or airline rules.\n"
    "2. Be concise (2-4 sentences).\n"
    "3. Cite the source at the end, like (source: 30_baggage.md).\n"
    "4. If a question asks to book, change, or cancel a specific flight, say this is "
    "handled by the booking tools and ask for the booking reference or flight number "
    "if none is given.\n"
    "5. If the CONTEXT does not answer the question, say you cannot find it in the "
    "knowledge base and suggest a rephrasing.\n"
    'CONTEXT start\n{context}\nCONTEXT end'
)

_STRIP = re.compile(r"\s+\s")
_NON_KEY = {"the", "a", "an", "is", "are", "what", "how", "can", "i", "me",
            "my", "do", "does", "of", "for", "to", "in", "on", "with", "and",
            "or", "be", "it", "at", "by", "am", "do"}


def _sig(t: str) -> str:
    return " ".join(sorted(_STRIP.sub(" ", t.lower()).split()))


def _ground_snippet(chunk_text: str, query: str, limit: int = 600) -> str:
    """Deterministic fallback: pick the bullet/line in the top chunk whose
    keywords best overlap the query, then its nearest neighbours."""
    keys = {w for w in re.findall(r"[a-z][a-z0-9'-]{2,}", query.lower())
            if w not in _NON_KEY}
    lines = [l.strip() for l in chunk_text.splitlines() if l.strip()]
    if not keys or len(lines) < 2:
        return _snip(chunk_text, limit)
    scored = [(len(keys & {w for w in re.findall(r"[a-z][a-z0-9'-]{2,}", l.lower())}), l)
              for l in lines]
    scored.sort(key=lambda x: -x[0])
    top_score = scored[0][0]
    if top_score == 0:
        return _snip(chunk_text, limit)
    picks = [l for sc, l in scored if sc == top_score][:2]
    picks = [re.sub(r"^[-•*]\s*|\d+\.\s*", "", p).strip() for p in picks]
    return "\n".join("- " + _STRIP.sub(" ", p) for p in picks)


def _snip(text: str, limit: int = 520) -> str:
    text = _STRIP.sub(" ", text).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    idx = max(cut.rfind(". "), cut.rfind("; "), cut.rfind(", "))
    return cut[: idx + 1] if idx > 80 else cut + "…"


def answer_from_context(query: str, context: str, sources: List[Dict[str, Any]]) -> Optional[str]:
    if not context.strip():
        return None
    user = f"USER QUESTION:\n{query}"
    try:
        return llm.complete(SYSTEM_PROMPT.format(context=context), user,
                            temperature=0.3, max_tokens=700)
    except Exception:
        return None


async def answer_knowledge(query: str, k: int = 3) -> Dict[str, Any]:
    """Full RAG answer pipeline. Always returns a dict with `answer` + `sources`."""
    q = (query or "").strip()
    if not q:
        return {"answer": "What would you like to know about your trip?", "sources": []}

    hits = get_rag().search(q, k)
    if not hits:
        return {
            "answer": ("I can't find that in the aviation knowledge base yet. "
                       "Ask me about baggage, security rules, refunds, rebooking, "
                       "airport codes, or booking a flight."),
            "sources": [],
        }

    context = "\n\n".join(
        f"[{h['source']} | {h['heading']}]\n{h['text']}" for h in hits
    )
    meta = [{"source": h["source"], "heading": h["heading"], "score": h["score"]}
            for h in hits]

    llm_text = await answer_from_context(q, context, meta)
    if llm_text:
        return {"answer": llm_text, "sources": meta}

    best = hits[0]
    snippet = _ground_snippet(best["text"], q)
    return {
        "answer": f"From the aviation knowledge base: {snippet}"
                  f" (source: {best['source']}, {best['heading']})",
        "sources": meta,
    }


def answer_knowledge_sync(query: str, k: int = 3) -> Dict[str, Any]:
    """Synchronous wrapper for executors that run outside an event loop."""
    import asyncio
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(answer_knowledge(query, k))
    return {"answer": None, "sources": []}  # caller should use the async form