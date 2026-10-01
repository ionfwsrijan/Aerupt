# AERUPT architecture

AERUPT is a **dual-path** real-time agent: a fast path produces immediate,
truthful, spoken/visual feedback; a slow path resolves tool plans and only
*commits* state-mutating work once things are unambiguous and interruption-safe.

## Event contract

Everything flows as timestamped events (`aerupt/protocol.py`):

- In: `start_of_turn`, `transcript` (with `end_of_turn`), `audio`, `frame`,
  `interrupt`, `tool_result`, `manifest`, `end`.
- Out (actions): `narration`, `filler`, `tool_call`, `cancel`,
  `clarify`, `response`.

`InterruptibleAgent` (`aerupt/agent.py`) is the facade: `submit()/ainput()`
push events, `run()` pumps until `end`, emitted actions go to `queue_out`
(override with a custom `send`). The harness binds a custom `send` and feeds
tool results back through the same handle.

## Fast path (spoken presence)

`Orchestrator._commit_turn` (per `end_of_turn`):

1. Merge ASR fragments (`_extend_turn_buffer`, handles VAD restarts by
   replacing a buffer that re-utters the same head), de-duplicate finals.
2. Parse + self-repair the utterance (see NLU below).
3. Commit the turn to a `StateSnapshot` — new facts override older slots
   (cross-turn correction, `s10`).
4. Build a plan (`Planner`). Narration reflects what we *actually* know:
   “Booking your trip to London” beats a generic “one sec”; fallback fillers
   rotate without repeating (`SpokenBrain.filler`).
5. Return control fast. Plans run as background tasks.

## Slow path (tool execution)

`Orchestrator._run_plan` loops:

- **(0) relaxation** — an empty search result re-issues the same query
  without the `date` constraint before downstream steps build (config
  `relax_empty_search`).
- **(a) issuance** — steps whose dependencies are *resolved* (not merely
  present) emit `tool_call`; state-mutating steps never issue until all deps
  resolved, so downstream arg builders see real payloads (`_payload_view`).
- **(b) retries** — read-only failures retried up to `max_retries`;
  retried state-mutating calls are deduped via the ledger.
- **(c) finalize** — all steps resolved → `response` with the snapshot.

### Interruption / supersession

A new user turn or `interrupt` bumps `epoch`. Plans check `epoch` on every
loop; a stale plan returns without acting. In-flight read-only tools get a
`cancel_grace_ms` (30 ms) then hard-cancel; the orchestrator then re-plans
from the updated snapshot. `s02` (retarget Tokyo) and `s03` (rebuild the
booking after an interrupt) exercise exactly this.

### Speculation

During partial ASR (`_schedule_speculate`), if the parsed partial satisfies a
whole read-only plan, AERUPT may issue the *first* call speculatively. Results
land in `spec_cache` keyed by `fingerprint(tool, args)` and are reused when
the committed turn re-asks the same query — so the answer is often already
warm when the user finishes speaking. Speculative calls never mutate state.

### Idempotency & safety

`aerupt/tools.py::IdempotencyLedger`:

- `fingerprint(tool, canonical_args)` — canonical arg order.
- Issue → mark issued; success → `done`; failure → `failed`; supersede →
  `cancelled`.
- `resolve_issue()` returns `done` (reuse result), `inflight` (don’t
  re-issue), or `new` (safe to issue).
- `MockEnv` treats a state-mutating tool re-issued after `done` as a
  duplicate and refuses it — AERUPT only reaches success once, proveably
  (`s03`, `s06`).
- Cancelling an existing booking (`s12`, `cancel_booking`) is another
  state-mutating operation: the env marks the booking cancelled (removing it
  from the duplicate-guard but preserving the record) and the ledger prevents
  double-cancels. `MockEnv._run_tool` routes `cancel*` tools before the
  generic “book” matcher so `cancel_booking` never books.

### Cross-epoch reuse guards

Empty search payloads are never cached in `spec_cache`, and the
`_issue_step` cross-epoch reuse of a completed read-only call refuses stale
EMPTY search results — so after an interrupt a follow-up re-searches (with
date relaxation still able to rescue a zero-result query) instead of reusing
a dead payload. Without this, `s03` regressed to ~43 at scale 1.0 when the
interrupt landed exactly as an empty search resolved.

### Isolated generation / stale defence

Per-user-turn: a cancelled call may be legitimately re-issued (new intent),
retries after a *failed* call are allowed, but re-issuing the same
`fingerprint(tool,args)` a second time in one generation with neither a
superseding cancel nor a failure in between is flagged stale (`scorer`
`_stale_reruns`). `s13`’s rapid-correction storm is exactly the legal regime:
every search is superseded by the next retarget and re-executed once.

## NLU (`aerupt/nlu.py`) — deterministic by design

- Manifest-driven: verbs/intents bootstrap from the tool manifest (`NLU.load_manifest`).
- **Self-repair**: `split_correction_segments` splits “Paris to… sorry, I
  mean Berlin” and flags post-marker segments as repairs; repair slot values
  override pre-repair ones, and the repair flag drives acknowledgements.
- Spatial slots: `from X to Y` / `to X from Y` with multi-word cities,
  trailing-junk trimming (`_city_trim`: “tokyo tomorrow” → “tokyo”, keeps
  “los angeles” whole) and alias mapping.
- Temporal: relative “tomorrow/next week” + absolute dates via
  `TemporalResolver`; reference date from the scenario.
- Counts: “2 tickets” via `_PAX_RE`; word-numbers near count keywords; the
  articles “a/an/single” are excluded from the count scan so “create a
  support ticket about…” never yields `passengers: 1`.
- `extract_count` keys are per-intent (passenger, adult, traveller, seat,
  ticket).
- **Verb anchors**: `detect_intent` prefers an explicit cancellation verb
  (“cancel/refund/void”) to the matching `cancel*` tool and an explicit
  booking verb to the book tool, so noun-score ties between `book_flight`
  and `cancel_booking` can never produce a wrong intent. Manifest keywords
  skip generic artifact nouns (flight/booking/… are recall *lenses*, not
  discriminators).

## Planning (`aerupt/planner.py`)

- `chain_auto_select`: a `book_flight` intent with no flight id
  automatically schedules `flight_search` first (chain deps), then picks the
  cheapest flight matching class/cabin constraints (`_pick_flight`).
- `_query_from_text`: query/question/issue/description params are derived
  from the utterance text (e.g. a manual lookup or `create_ticket` subject).
- Generic/intent params map from slot values; unmatched required params →
  one clarification at a time (`clarify_limit: 1`).
- `cancel_booking` is a plain single-step intent: no implicit search is
  inserted (it is not a "book"), and its `flight_id` grounds from the slot
  carried by the session snapshot.
- **Multi-turn slot hygiene** (`_book_args.build`): when a chained search runs
  this turn, the book step prefers the *fresh* search results (via `_pick_flight`),
  so a flight id carried from an earlier turn's booking can never leak into a
  new route. A flight code is only honored if it is literally spoken this turn
  (`_explicit_fid` regex `\b[A-Za-z]{1,3}\d{2,4}\b`). The progress narration
  applies the same rule: carried ids are never announced.

## Grounding (`aerupt/grounding.py`, `aerupt/speech.py`)

- Audio: base64 WAV ingestion → silence detection → VAD segmentation.
- Frames: base64 PNG (or hex fallback) → gray-scaled thumbnail → region text.
- Frames picked up by `_on_frame` prime a `session.last_frame_desc` that a
  subsequent manual-lookup tool call can reference (`s08`).
- **Spoken presence**: every committed turn narrates what it actually knows
  (`narrate_intent`); unseen-manifest tools get a truthful generic progress
  line; when rapid corrections would otherwise be throttled by the narration
  spam-guard, a short interrupt-ack keeps the first-spoken time immediate
  (`s10`, `s13`).

## Environment & harness

- `harness/harness.py::ScenarioRunner` replays timestamped events on a shared
  `Clock(scale)`; `send` routes `tool_call`/`cancel` to `MockEnv`, whose
  `tool_result` events are fed back into the agent loop.
  - **Scale-invariant pacing**: event deltas are passed to `clock.sleep`
    unscaled (the clock applies `scale` once), which keeps events and tool
    latencies moving at the same factor at every `--scale` — relative
    interrupt timing is therefore reproducible at 0.2 / 0.4 / 1.0 alike.
  - **Idle barrier**: after the last input, the harness stays alive (up to a
    real-time cap) while `MockEnv` still has in-flight calls, so the final
    epoch always consumes its last tool result and emits its response before
    teardown.
- `harness/mockenv.py`: `FLIGHT_DB` (10 rows on the reference date),
  fault injection (`fail_first_search`), latency per tool, call_records with
  ok/error + `done_ts`, bookings/tickets/cancelled_bookings, `active` counter,
  and duplicate-booking rejection.
- `harness/scorer.py`: Task / Interruption / Latency / Safety sum → weighted
  quality multiplier (0.8–1.2) driven by how substantially the response
  nails intent/slots/flight ids; multimodal scenarios carry a 1.5x flag in
  the hidden kit. `score()` also returns `checks_report` — a per-check
  pass/fail list exploded by `run_harness --detail` and the sweep.

## Browser UI (`ui/server`, `ui/client`, `run_ui.py`)

- FastAPI app (`ui/server/main.py`): one WebSocket per browser session, plus
  `/api/health` (reports `agent`, `version`, `platform` via
  `active_provider().name`, and `mode` via `load_llm_config().human_label` =
  `groq:<model>` / `deterministic`) and static serving of the built React app
  from `ui/client/dist`.
- `ui/server/session.py` bridges the live agent into a websocket: a
  `route_out` synchronously forwards every orchestrator action to both the
  async channel consumed by the mock environment (so tools actually resolve)
  and an outbox pushed to the browser. Tool results re-enter the agent as
  regular environment events and are also emitted to the UI, tagged with the
  tool name so the client can render per-tool cards.
- The client (`ui/client/`) renders a vertical event feed: narration/typewriter
  text, per-tool cards (running → done/cancelled), clarify quick-reply chips,
  and interrupt buttons. The empty chat state offers quick-action suggestion
  chips (commit via the same `pushUser` + `commit` path as typed turns).
  `end_of_turn:false` transcripts exercise the
  speculation path; `reset` tears down and re-arms the agent session.

## Hybrid LLM + RAG layer (aviation Q&A)

The deterministic core stays the safety/latency backbone; knowledge answers are
a *grounded* layer on top so the rubric-scoring paths never depend on a network
round-trip.

- `aerupt/llm.py` — thin OpenAI-compatible client for **Groq** (`complete()`).
  Reads `GROQ_API_KEY`/`LLM_API_KEY`, `GROQ_MODEL` (default
  `llama-3.3-70b-versatile`), `LLM_BASE_URL`, `LLM_TIMEOUT_S` (8). Any failure
  (no key, timeout, HTTP error) returns `None` → deterministic fallback. Never
  blocks a turn: `AERUPT_LLM=0` disables it outright.
- `knowledge/*.md` — 9-file aviation corpus (booking, airports, baggage,
  security, refunds, disruption, frequent-flyer, glossary, routes/fares).
- `aerupt/rag.py` — heading-aware markdown chunker (49 chunks), `RetrievalIndex`
  over TF-IDF cosine similarity (numpy). `AERUPT_RAG_EMBEDDINGS=sentence`
  switches to a cached sentence-transformer encoder (`AERUPT_RAG_SENTENCE_MODEL`,
  default `all-MiniLM-L6-v2`). `AERUPT_KB_DIR` overrides the corpus,
  `AERUPT_KB_TOP_K` the default hit count. `RAGEngine.search(q, k)` returns
  chunks with source + heading + score; the same module doubles as a CLI
  (`python -m aerupt.rag "…" --top-k 5` → `aerupt-kb` console script).
- `aerupt/responder.py::answer_knowledge` — top-k chunks → strict system prompt
  (“answer only from the snippets, cite sources”) → LLM, else
  `_ground_snippet` (deterministic keyword-overlap bullet picker). Always
  returns `{"answer", "sources"}`.
- Routing: `kb_lookup` and `flight_status` are UI/demo-only tools —
  `FLIGHT_MANIFEST_UI` lives in `harness/scenarios.py` (shared by the console
  demo and the websocket server session), layered on the shared
  `FLIGHT_MANIFEST` so the harness evaluation is untouched. NLU has an explicit
  KB anchor (policy/rule/
  baggage/security/TSA words; “can/could/am I bring…”, “do I need…”, “how
  many/much/big/…”) that beats noun-score ties, `Planner._knowledge_args`
  passes the *raw* utterance as `query` (the generic `_query_from_text`
  mangles natural questions), and `MockEnv._run_tool` answers it via
  `responder.answer_knowledge`. The UI tool card for `kb_lookup` shows the
  source files that grounded the answer.
- Live tracking: a flight id (`\b[a-z]{1,3}\d{2,4}\b`) or a flight/departure
  word plus a status verb (delayed/boarding/on time/tracked…) anchors
  `flight_status` ahead of the KB anchor — but a policy question with no
  flight reference (“baggage delay policy”) still routes to the KB.
  `MockEnv._flight_status` derives status/gate/terminal deterministically from
  the flight id so repeated queries agree and nothing is hallucinated.
- Refund grounding: `MockEnv._cancel_booking` returns `refund_pct`/
  `refund_amount` for a cancelled refundable booking — cabin-class shares
  (economy 72% / business 88% / first 95%) from `REFUND_RATES`, mirrored in
  `knowledge/50_refunds.md` so tool numbers and the cited policy stay
  consistent. The narration/reply contract (reference + flight_id) is
  unchanged.

The tool is named `kb_lookup` rather than `knowledge_search`: the latter’s
`search` token collided with `flight_search` in the manifest-driven scorer and
tripped its ambiguity guard for plain “search flights…” requests.

## Flight platform providers (`aerupt/providers/`)

Execution is fronted by `BookingProvider`:

- `active_provider()` is a process-wide factory: `AERUPT_PLATFORM=mock` (default)
  → `MockBookingProvider` (deterministic references, no network — byte-identical
  behaviour to what the harness evaluates); `AERUPT_PLATFORM=amadeus` →
  `AmadeusProvider` if `AMADEUS_CLIENT_ID`/`AMADEUS_CLIENT_SECRET` are present
  (`configured`), else the mock. `reset_provider_cache()` reloads after env
  changes.
- `AmadeusProvider` — OAuth2 client-credentials token cache; live search
  (GET `/v2/shopping/flight-offers`), booking (POST `/v1/booking/flight-orders`,
  offer cached from the prior search), cancellation
  (DELETE `/v1/booking/flight-orders/{id}`). Results are normalized into the
  internal flight shape (`_offer_to_flight`); every call returns
  `{"ok", ...}/{ "ok": false, "error" }` so a sandbox outage degrades to the
  same error-handling path as `MockEnv`’s fault injection.
- `MockEnv._run_tool` routes `search*`/`cancel*`/`book*` to the live provider
  *only* when `_live_provider()` is non-None (platform is **and** configured);
  otherwise the inline sandbox runs — so with no credentials the harness,
  e2e, and UI all execute the exact evaluated code path. `status*` tools
  always resolve inline (`_flight_status`), safe to execute beside a live
  provider.

## Config knobs (`aerupt/config.py`)

- `load_env()` — best-effort `.env` loading (python-dotenv, optional). Called
  from `run_ui.py`, `run_demo.py`, `run_harness.py`, and the server entrypoint
  so every knob can live in a single `.env` file.
- Timings: `ack_delay_ms`, `intro_narrate_ms`, `progress_filler_ms`,
  `long_task_filler_ms`, `interrupt_ack_ms`, `cancel_grace_ms`,
  `retry_backoff_ms`.
- Behavior: `speculation_enabled`, `require_complete_args_to_speculate`,
  `chain_auto_select`, `relax_empty_search`, `max_retries`,
  `idempotency_window_s`.
- All timings are virtualized via `Clock(scale)` so `run_harness` can sprint
  (demo) or run at 1x without changing agent semantics.