"""Retrieval-augmented generation core for the aviation knowledge base.

A chunked tier:
  1. `knowledge/*.md` is split into titled chunks (heading per section).
  2. `RetrievalIndex` builds TF–IDF vectors (numpy) over the chunk vocabulary.
  3. `RAGEngine.search` returns the top-k most relevant chunks for a query.

Optional upgrade: set `AERUPT_RAG_EMBEDDINGS=sentence` to use a local
sentence-transformer encoder (default `all-MiniLM-L6-v2`, override with
`AERUPT_RAG_SENTENCE_MODEL`) when a cached model is available. If it cannot
load, the TF–IDF index is used — retrieval always works offline and
deterministically.

Configuration (environment):
  AERUPT_KB_DIR           directory of `*.md` sources (default `knowledge/`)
  AERUPT_KB_TOP_K         default number of chunks per search (default 3)
  AERUPT_RAG_EMBEDDINGS   "sentence" to use local embeddings, else tfidf
  AERUPT_RAG_SENTENCE_MODEL  sentence-transformer model id (optional)

Run as a CLI:  python -m aerupt.rag "carry-on bag size" --top-k 5
"""
from __future__ import annotations

import argparse
import math
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_KB_DIR = PROJECT_ROOT / "knowledge"
DEFAULT_TOP_K = 3
DEFAULT_SENTENCE_MODEL = "all-MiniLM-L6-v2"


def _default_kb_dir() -> Path:
    return Path(os.environ.get("AERUPT_KB_DIR", str(DEFAULT_KB_DIR))).resolve()


def _default_top_k() -> int:
    try:
        return max(1, int(os.environ.get("AERUPT_KB_TOP_K", str(DEFAULT_TOP_K))))
    except ValueError:
        return DEFAULT_TOP_K

_TOKEN_RE = re.compile(r"[a-z][a-z0-9]*")


@dataclass
class Chunk:
    id: str
    source: str
    heading: str
    text: str


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------
def chunk_markdown(text: str, source: str) -> List[Chunk]:
    chunks: List[Chunk] = []
    current_h: str = "overview"
    buffer: List[str] = []
    seq = 0

    def flush() -> None:
        nonlocal buffer, seq
        body = re.sub(r"\n{2,}", "\n", "\n".join(buffer)).strip()
        if body:
            chunks.append(Chunk(id=f"{source}:{seq}", source=source,
                                heading=current_h, text=body))
            seq += 1
        buffer = []

    for line in text.splitlines():
        if line.startswith("#"):
            flush()
            h = line.lstrip("#").strip()
            if h:
                current_h = h
        elif line.strip():
            buffer.append(line.strip())
    flush()
    return chunks


def load_corpus(kb_dir: Path) -> List[Chunk]:
    chunks: List[Chunk] = []
    for md in sorted(kb_dir.glob("*.md")):
        try:
            raw = md.read_text(encoding="utf-8")
        except OSError:
            continue
        chunks.extend(chunk_markdown(raw, md.name))
    return chunks


# ---------------------------------------------------------------------------
# Retrieval index
# ---------------------------------------------------------------------------
def _tokens(text: str) -> List[str]:
    return [t for t in _TOKEN_RE.findall(text.lower()) if len(t) >= 2]


class RetrievalIndex:
    def __init__(self, chunks: List[Chunk], use_embeddings: bool = False):
        self.chunks = chunks
        self._use_embeddings = use_embeddings and _sentence_model_ok()
        self._vocab: Dict[str, int] = {}
        self._vecs: Optional[np.ndarray] = None
        self._idf: Optional[np.ndarray] = None

    @property
    def mode(self) -> str:
        return "sentence-embeddings" if self._use_embeddings else "tfidf"

    def _build_tfidf(self) -> None:
        n = len(self.chunks)
        dfs: Dict[str, int] = {}
        rows: List[Dict[str, int]] = []
        for c in self.chunks:
            toks = _tokens(c.text)
            counts: Dict[str, int] = {}
            for t in toks:
                counts[t] = counts.get(t, 0) + 1
            rows.append(counts)
            for t in set(counts):
                dfs[t] = dfs.get(t, 0) + 1
        vocab = sorted(dfs)
        self._vocab = {t: i for i, t in enumerate(vocab)}
        idf = np.zeros(len(vocab), dtype=np.float64)
        for t, df in dfs.items():
            idf[self._vocab[t]] = math.log((1.0 + n) / (1.0 + df)) + 1.0
        mat = np.zeros((n, len(vocab)), dtype=np.float32)
        for i, counts in enumerate(rows):
            for t, c in counts.items():
                tf = 1.0 + math.log(c)
                mat[i, self._vocab[t]] = tf * idf[self._vocab[t]]
        norms = np.linalg.norm(mat, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        self._vecs = mat / norms
        self._idf = idf

    def _build_sentence(self) -> None:
        try:
            enc = _load_st().encode([c.text for c in self.chunks],
                                    normalize_embeddings=True)
            self._vecs = np.asarray(enc, dtype=np.float32)
        except Exception:
            self._use_embeddings = False
            self._build_tfidf()

    def build(self) -> "RetrievalIndex":
        if not self.chunks:
            self._vecs = np.zeros((0, 1), dtype=np.float32)
            self._idf = np.ones(1, dtype=np.float64)
            return self
        if self._use_embeddings:
            self._build_sentence()
        else:
            self._build_tfidf()
        return self

    def search(self, query: str, k: int = 3) -> List[Dict[str, Any]]:
        k = max(1, k)
        if not self.chunks or self._vecs is None:
            return []
        if self._use_embeddings:
            try:
                q = np.asarray(_load_st().encode([query], normalize_embeddings=True),
                               dtype=np.float32).reshape(-1)
            except Exception:
                return self._search_tfidf(query, k)
        else:
            q = self._query_vec(query)
        scores = self._vecs @ q
        order = np.argsort(-scores)
        hits: List[Dict[str, Any]] = []
        for i in order[: k]:
            sc = float(scores[i])
            if sc <= 0:
                break
            c = self.chunks[int(i)]
            hits.append({"id": c.id, "source": c.source, "heading": c.heading,
                         "text": c.text, "score": round(sc, 4)})
        return hits

    def _search_tfidf(self, query: str, k: int) -> List[Dict[str, Any]]:
        self._use_embeddings = False
        return self.search(query, k)

    def _query_vec(self, query: str) -> np.ndarray:
        vec = np.zeros(len(self._vocab), dtype=np.float32)
        for t, c in _counts(_tokens(query)).items():
            if t in self._vocab:
                vec[self._vocab[t]] = (1.0 + math.log(c)) * self._idf[self._vocab[t]]
        nrm = np.linalg.norm(vec)
        return vec / nrm if nrm > 0 else vec


def _counts(toks: List[str]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for t in toks:
        out[t] = out.get(t, 0) + 1
    return out


_ST_MODEL: Any = None


def _load_st():
    global _ST_MODEL
    if _ST_MODEL is None:
        from sentence_transformers import SentenceTransformer
        model = os.environ.get("AERUPT_RAG_SENTENCE_MODEL", DEFAULT_SENTENCE_MODEL)
        _ST_MODEL = SentenceTransformer(model)
    return _ST_MODEL


def _sentence_model_ok() -> bool:
    return os.environ.get("AERUPT_RAG_EMBEDDINGS", "").strip().lower() == "sentence"


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------
class RAGEngine:
    def __init__(self, kb_dir: Optional[Path] = None, use_embeddings: Optional[bool] = None,
                 top_k: Optional[int] = None):
        self.kb_dir = kb_dir or _default_kb_dir()
        self.use_embeddings = use_embeddings if use_embeddings is not None \
            else _sentence_model_ok()
        self.top_k = top_k or _default_top_k()
        self._index: Optional[RetrievalIndex] = None

    def index(self) -> RetrievalIndex:
        if self._index is None:
            self._index = RetrievalIndex(load_corpus(self.kb_dir),
                                         self.use_embeddings).build()
        return self._index

    @property
    def mode(self) -> str:
        return self.index().mode

    def __len__(self) -> int:
        return len(self.index().chunks)

    def search(self, query: str, k: int = 3) -> List[Dict[str, Any]]:
        return self.index().search(query, k)


_RAG: Optional[RAGEngine] = None


def get_rag() -> RAGEngine:
    global _RAG
    if _RAG is None:
        _RAG = RAGEngine()
        _RAG.index()
    return _RAG


def search(query: str, k: int = DEFAULT_TOP_K) -> List[Dict[str, Any]]:
    return get_rag().search(query, k or _default_top_k())


# ---------------------------------------------------------------------------
# Command-line interface
# ---------------------------------------------------------------------------
def cli(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="aerupt-kb",
        description="Search the AERUPT aviation knowledge base.")
    ap.add_argument("query", help="natural-language aviation question")
    ap.add_argument("--top-k", type=int, default=None,
                    help=f"chunks to return (default {DEFAULT_TOP_K})")
    ap.add_argument("--kb-dir", default=None, help="override the KB directory")
    ap.add_argument("--embeddings", action="store_true",
                    help="force the sentence-embedding index")
    args = ap.parse_args(argv)

    engine = RAGEngine(kb_dir=Path(args.kb_dir) if args.kb_dir else None,
                       use_embeddings=args.embeddings,
                       top_k=args.top_k)
    hits = engine.index().search(args.query, engine.top_k)
    print(f"[AERUPT KB] mode={engine.mode} sources={len(engine):d} "
          f"query={args.query!r} hits={len(hits)}")
    if not hits:
        print("  no match — try rephrasing (e.g. 'carry-on bag size limit').")
        return 1
    for h in hits:
        snippet = " ".join(h["text"].split())[:140]
        print(f"\n  [{h['score']:.3f}] {h['source']} — {h['heading']}")
        print(f"      {snippet}")
    return 0


if __name__ == "__main__":
    sys.exit(cli())