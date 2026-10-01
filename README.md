# AERUPT — Interruptible Real-Time Agent

[![CI](https://github.com/ionfwsrijan/Aerupt/actions/workflows/ci.yml/badge.svg)](https://github.com/ionfwsrijan/Aerupt/actions/workflows/ci.yml)

Voice-native assistant for the **Samsung Prism GenAI · Theme 05 — Interruptible
Real-Time Agents** challenge. AERUPT speaks over timestamped event queues, treats
voice/video/gesture as continuous input, and can be safely interrupted mid-task.

## Quick start

```powershell
python -m venv .venv && .venv\Scripts\Activate.ps1   # optional but recommended
pip install -r requirements.txt                       # core deps
py run_harness.py            # score all scenarios (see notes below)
py run_harness.py s03        # score one scenario
py run_harness.py s08 --scale 0.8 --no-save
py run_demo.py               # interactive console demo
py run_ui.py                 # browser UI on http://127.0.0.1:8000
py -m unittest discover -s tests
```

AERUPT auto-loads a `.env` file when present (`python-dotenv` is optional;
without it every knob below can be exported in the shell instead). Copy
`.env.example` and fill in optional API keys — the agent runs fully offline
without any of them.

Try the knowledge base from the terminal:

```powershell
python -m aerupt.rag "carry-on bag size limit" --top-k 5
# or, if installed as a package:  aerupt-kb "can I bring a power bank?"
```

Windows note: use `py` (the Python launcher), not `python`.

## Browser UI

A FastAPI + WebSocket backend serves a React UI (Vite + TypeScript + Tailwind +
framer-motion + lucide-react) over the same agent pipeline — running tool cards,
typewriter narration, barge-in/stop interrupts, clarification quick-replies,
live transcript for real microphone input, spoken responses (local TTS with a
header mute toggle — a barge-in cuts the voice mid-sentence), and a vertical
timeline. The empty chat state offers **quick-action chips** (book, retarget,
flight status, knowledge) that commit the same way a typed turn does.

`GET /api/health` reports what stack the server is actually running:
`agent`, `version`, `platform` (`mock` / `amadeus`), and `mode`
(`groq:<model>` when a Groq key is configured, else `deterministic`).

```powershell
py run_ui.py                            # serve on http://127.0.0.1:8000
py run_ui.py --port 9000                # custom port
py run_ui.py --reload                   # auto-reload backend on edits
```

Build the frontend once (assets are served from `ui/client/dist` when present):

```powershell
cd ui/client
npm install
npm run build
```

During frontend development you can run `npm run dev` (Vite dev server proxies
`/ws` and `/api` to the backend).

## What you get

- `aerupt/` — the agent (deterministic NLU, planner, orchestrator, voice/vision
  grounding, config). No network, no LLM keys, fully reproducible.
- `harness/` — timestamped scenario replay, a mock tool environment, and a
  scorer that mirrors the rubric (Task 40% / Interruption 35% / Latency 15% /
  Safety 10%), with 1.5x weight flags for multimodal scenarios.
- `ui/server/` + `ui/client/` — WebSocket session bridge and the React UI.
- `run_harness.py` — CLI runner: prints per-scenario and aggregate scores.
- `run_demo.py` — interactive console loop with the same pipeline.
- `run_ui.py` — launches the browser UI.
- `tests/` — unit tests (NLU extraction, ledger idempotency, planner chains,
  multi-turn slot hygiene, end-to-end scenarios).
- `knowledge/` + `aerupt/rag.py`, `aerupt/llm.py`, `aerupt/responder.py` — aviation
  KB and the hybrid LLM + RAG answer layer (see below).

## Hybrid LLM + RAG (aviation Q&A)

AERUPT is an **aviation** assistant: alongside flight search/booking/cancellation
it answers policy questions from a curated aviation knowledge base
(`knowledge/*.md` — baggage, security, refunds, airports, disruption, frequent
flyer, glossary). The architecture is hybrid:

- The deterministic NLU → planner → orchestrator core stays the safety/latency
  backbone (it is what the 13-scenario rubric scores).
- A **Groq LLM** (OpenAI-compatible) makes final answers grounded: retrieval
  returns top-k chunks, the LLM answers solely from them (with citations).
- **Retrieval** is TF-IDF by default; set `AERUPT_RAG_EMBEDDINGS=sentence` to use
  a local sentence-transformer encoder.
- No API key? No problem — a deterministic keyword-overlap snippet picker
  answers from the same chunks, and the agent never blocks on the network
  (8-second cap, then fallback).

Aviation questions hit the `kb_lookup` tool over the same WebSocket pipeline,
and the UI shows the knowledge-base sources that grounded each answer.
Demo: *“What is the carry-on limit?”*, *“Am I allowed to bring a power bank?”*.

## Flight platforms

Tool execution routes through a single `BookingProvider` abstraction
(`aerupt/providers/`):

| `AERUPT_PLATFORM` | Behaviour |
| --- | --- |
| `mock` (default) | In-repo sandbox that is byte-identical to the evaluated harness |
| `amadeus` | Real **Amadeus Self-Service API** — OAuth2, live flight offers, booking orders, cancellations (test sandbox by default) |

Amadeus mode activates only when credentials are present; otherwise execution
stays on the sandbox. Copy `.env.example` and set:

```powershell
# in the shell, or a .env loaded by your tooling
$env:AERUPT_PLATFORM = "amadeus"
$env:AMADEUS_CLIENT_ID = "…"
$env:AMADEUS_CLIENT_SECRET = "…"
$env:GROQ_API_KEY = "…"      # for grounded knowledge answers
```

`AMADEUS_BASE_URL` defaults to `https://test.api.amadeus.com`; use
`https://api.amadeus.com` for production credentials.

## Interactive flight tools

The browser UI and console demo run the canonical three-flight-tool manifest
(`flight_search` / `book_flight` / `cancel_booking`) **plus** two read-only
interactive tools that stay out of the evaluated harness manifest:

- **`flight_status`** — deterministic live tracking (status, gate, terminal)
  for any spoken flight id, e.g. *“Is my flight SU450 on time?”*.
- **`kb_lookup`** — the RAG-backed aviation knowledge tool (below).

Cancelling a booking returns a **refund grounding**: a refund share per cabin
class (economy 72% / business 88% / first 95%) computed from the fare on file
and cross-checked against `knowledge/50_refunds.md`, plus the refunded amount —
so the agent's numbers always match the policy it cites.

## Scoring scenarios

```powershell
py run_harness.py --scale 0.4
```

Every scenario re-runs with `Clock(scale=…)` so tool latencies can be tuned
for demo speed while real wall-time latency checks stay meaningful. Event
pacing and tool latencies are scaled by the *same* factor, so relative timing
(interrupts vs. in-flight calls) is invariant to `--scale`; `--sweep` runs the
whole suite at several scales as a regression gate. The canonical suite
currently scores **100.0 across all 13 scenarios, 9/9 runs each**:

| scenario | what it exercises |
| --- | --- |
| s01 simple chain booking | search → auto-select → book |
| s02 interrupt retarget destination | redirect mid-plan, stale search cancelled |
| s03 interrupt mid-booking adjust | change seats/class, rebook cleanly |
| s04 speech repair disfluency | “Paris to… sorry, I mean Berlin” |
| s05 chained clarify fill | staged slot filling via clarification |
| s06 retry fault injection | upstream timeout retried, never duplicated |
| s07 unseen tool rent_car | unseen manifest tool handled safely |
| s08 visual frame grounded manual | camera feed grounds a manual lookup |
| s09 audio first then action | audio capture before the spoken request |
| s10 session slot correction | cross-turn slot override mid-session |
| s11 support ticket | free-text subject → idempotent `create_ticket` |
| s12 cancel booking | confirm a booking, then `cancel_booking` it |
| s13 rapid correction storm | burst of retargets; only the last commits |

## Scoring scenarios

```powershell
py run_harness.py --scale 0.4     # sprinted suite
py run_harness.py s03 --detail    # per-check breakdown for one scenario
py run_harness.py --sweep 3       # robustness gate: 3 runs × scales (0.2,0.4,1.0)
py run_harness.py --profile       # real-time profile: duration, t2s, per-tool latency
```

`--detail` prints a per-check pass/fail report (`checks_report`) for sub-100
scores; `--sweep` flags any scenario that is not 100.0 at every scale as a
regression; `--profile` surfaces time-to-first-spoken (t2s), narration counts,
cancel counts, and per-tool average latency so UX gaps are visible at a glance.

## Design in one breath

Fast/slow dual-path execution with an **epoch** counter: a new user turn bumps
the epoch, orphaned plans exit, in-flight read-only tools get a 30 ms grace
period then cancel, and a speculative single-step *read-only* tool call may
issue during partial ASR so the answer is typically already warming when the
turn commits. State-mutating calls (book/ticket/create/cancel) go through an
**idempotency ledger** keyed by `fingerprint(tool, canonical_args)` so a retry
or a conflicting interrupt can never double-book. Empty search results are
re-run without the date constraint (relaxation) before downstream steps are
issued. An **idle barrier** keeps the harness alive until every in-flight tool
has landed, so the final epoch always emits its answer. The spoken surface
narrates truthfully on every turn (with short acks during rapid-correction
storms) so first-spoken time stays immediate. See `ARCHITECTURE.md`.

## Scoring notes / assumptions

- The repository scorer is a local standalone implementation. The official
  kit may score differently; treat the runner as a fast internal signal, not
  the final word.
- Scenarios are deterministic: the same clock scale and the same event
  schedule produce the same result. Interruption-timing checks are stable at
  the shipped `--scale 0.4` and at `1.0`.