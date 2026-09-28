"""Coordination Layer.

Runs the Fast Path (reflexes) and Slow Path (chained tools + reasoning) on a
unified async timeline, plus:

  - epoch-based generation control: any interrupt or superseding turn bumps
    the epoch and invalidates older in-flight work
  - grace-period cancellation: a superseded call is given a few ms to either
    land (salvageable) or die; only then do we emit `cancel`
  - idempotency: state-changing calls are fingerprint-deduped so a mid-
    booking correction can never double-book
  - speculation: early read-only calls on partial transcripts are reused when
    they still match the final intent (latency hiding), otherwise cancelled
  - session slot tracking through StateSnapshot.apply_turn localized merges

Interface: the agent feeds `Event`s in and `handle()` routes them; emitted
Actions go through `self._send`.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import re
from typing import Any, Dict, List, Optional

from .config import Config
from .grounding import Grounding
from .nlu import NLU
from .planner import MISSING, Plan, Planner, Step
from .protocol import (ACTION_CLARIFY, ACTION_FILLER, ACTION_NARRATION,
                       ACTION_RESPONSE, EVENT_AUDIO, EVENT_END, EVENT_FRAME,
                       EVENT_INTERRUPT, EVENT_MANIFEST, EVENT_START_OF_TURN,
                       EVENT_TOOL_RESULT, EVENT_TRANSCRIPT, Action, Event)
from .snapshot import StateSnapshot
from .speech import SpokenBrain
from .tools import IdempotencyLedger, ToolRegistry
from .utils import Clock, Throttle, fingerprint

CLARIFY = object()


class CallRecord:
    __slots__ = ("call_id", "tool", "args", "fp", "epoch", "speculative",
                 "state_modifying", "cancelled", "ok", "result", "error",
                 "done_event", "issued_at", "retries", "alive")

    def __init__(self, call_id: str, tool: str, args: Dict[str, Any],
                 fp: str, epoch: int, speculative: bool, state_modifying: bool,
                 clock: Clock):
        self.call_id = call_id
        self.tool = tool
        self.args = args
        self.fp = fp
        self.epoch = epoch
        self.speculative = speculative
        self.state_modifying = state_modifying
        self.cancelled = False
        self.ok = False
        self.result: Any = None
        self.error: Any = None
        self.done_event = asyncio.Event()
        self.issued_at = clock.now()
        self.retries = 0
        self.alive = True


class Session:
    def __init__(self):
        self.last_flights: List[Any] = []
        self.last_frame_ref: Optional[str] = None
        self.last_frame_desc: Optional[str] = None


class Orchestrator:
    def __init__(self, config: Optional[Config] = None,
                 send: Optional[Any] = None, clock: Optional[Clock] = None):
        self.cfg = config or Config()
        self._send = send or (lambda _p: asyncio.sleep(0))
        self.clock = clock or Clock(scale=self.cfg.time_scale)
        self.nlu = NLU()
        self.registry = ToolRegistry()
        self.ledger = IdempotencyLedger(window_s=self.cfg.idempotency_window_s)
        self.snapshot = StateSnapshot()
        self.brain = SpokenBrain()
        self.grounding = Grounding()
        self.planner = Planner(self.nlu, self.registry)
        self.session = Session()

        # - turn state
        self.in_turn = False
        self.turn_buffer = ""
        self.turn_no = 0
        self.turn_started_at = 0.0
        self.last_commit_ts = -999.0
        self.last_committed_text = ""
        self.last_text_event_ts = -999.0

        # - epochs / concurrency
        self.epoch = 0
        self.active: Optional[Dict[str, Any]] = None
        self.stop = False
        self.wake = asyncio.Event()

        # - call tracking
        self.recs: Dict[str, CallRecord] = {}
        self._fp_index: Dict[str, str] = {}
        self.spec_cache: Dict[str, Any] = {}
        self.outstanding_cancel_tasks: List[asyncio.Task] = []
        self.tasks: List[asyncio.Task] = []

        self.trace: List[Dict[str, Any]] = []
        self._spoken_throttle = Throttle(900.0)
        self._ack_throttle = Throttle(1800.0)

    # ==================================================================
    # Entry
    # ==================================================================
    async def handle(self, ev: Event) -> None:
        if self.stop and ev.type != EVENT_END:
            return
        kind = ev.type
        if kind == EVENT_START_OF_TURN:
            self._begin_turn(ev)
        elif kind == EVENT_TRANSCRIPT:
            await self._on_transcript(ev)
        elif kind == EVENT_MANIFEST:
            self._on_manifest(ev)
        elif kind == EVENT_AUDIO:
            await self._on_audio(ev)
        elif kind == EVENT_FRAME:
            await self._on_frame(ev)
        elif kind == EVENT_INTERRUPT:
            await self._on_interrupt(ev)
        elif kind == EVENT_TOOL_RESULT:
            await self._on_tool_result(ev)
        elif kind == EVENT_END:
            self._end(ev)

    def begin_scenario(self, today: Any = None) -> None:
        if today is None:
            return
        try:
            y, m, d = (int(x) for x in str(today).split("-"))
            ref = _dt.date(y, m, d)
            self.snapshot = StateSnapshot(ref)
            self.nlu.set_today(ref)
            self.brain._ref = ref
        except Exception:
            pass

    # ==================================================================
    # Fast path: turn lifecycle
    # ==================================================================
    def _begin_turn(self, ev: Event) -> None:
        self.in_turn = True
        self.turn_buffer = ""
        self.turn_started_at = self.clock.now()

    async def _on_transcript(self, ev: Event) -> None:
        d = ev.data
        text = str(d.get("text") or d.get("transcript") or d.get("raw") or "")
        text = re.sub(r"\s+", " ", text).strip()
        eot = bool(d.get("end_of_turn") or d.get("final")
                   or d.get("eot") or d.get("is_final"))
        if not self.in_turn:
            self.in_turn = True
            self.turn_buffer = ""
            self.turn_started_at = self.clock.now()
        if text:
            self.turn_buffer = self._extend_turn_buffer(text)
        self.last_text_event_ts = self.clock.now()

        if eot:
            if not text and not self.turn_buffer:
                self.in_turn = False
                return
            if self._is_dup_final():
                self.in_turn = False
                return
            await self._commit_turn()
        else:
            self._schedule_speculate()

    def _extend_turn_buffer(self, text: str) -> str:
        """Append a transcript fragment, handling ASR restarts.

        Voice engines often re-utter the current segment from scratch
        (VAD restart). If the new text starts the same way as what we
        already buffered, it is an update of the SAME utterance — replace
        instead of concatenate.
        """
        cur = (self.turn_buffer or "").strip()
        text = (text or "").strip()
        if not cur:
            return text

        def head(t: str, n: int = 4) -> tuple:
            return tuple(t.split()[:n])

        if head(text) and head(text) == head(cur):
            return text
        return " ".join((cur, text)).strip()

    def _is_dup_final(self) -> bool:
        same = self.turn_buffer == self.last_committed_text
        fresh = (self.clock.now() - self.last_commit_ts) < 0.7
        return same and fresh

    def _schedule_speculate(self) -> None:
        if not self.cfg.speculation_enabled or not self.in_turn:
            return
        t = asyncio.create_task(self._speculate())
        self.tasks.append(t)

    async def _speculate(self) -> None:
        try:
            await self.clock.sleep(self.cfg.speculation_min_ms * 0.001)
            if not self.in_turn or self.stop:
                return
            if any(r.speculative and r.epoch == self.epoch for r in self.recs.values()):
                return
            text = self.turn_buffer or ""
            if len(text) < 6:
                return
            parse = self.nlu.parse_turn(text)
            intent = parse.get("intent")
            if not intent:
                return
            spec = self.registry.get(intent)
            if spec is None or spec.state_modifying:
                return  # never speculate on state-changing work
            args, missing = self.planner.map_args(spec, parse.get("slots", {}), text)
            if missing and self.cfg.require_complete_args_to_speculate:
                return
            if not args:
                return
            args = self._enrich_args(intent, args)
            if not args:
                return
            fp = fingerprint(intent, args)
            if fp in self.spec_cache:
                return
            if fp in self._fp_index:
                return
            call_id = self.registry.next_call_id(intent)
            rec = CallRecord(call_id, intent, args, fp, self.epoch,
                             speculative=True, state_modifying=False,
                             clock=self.clock)
            self.recs[call_id] = rec
            self._index_fp(rec)
            await self._emit_tool_call(call_id, intent, args, speculative=True)
        except asyncio.CancelledError:
            pass

    # ==================================================================
    # Turn commit -> slow path
    # ==================================================================
    async def _commit_turn(self) -> None:
        self.turn_no += 1
        text = self.turn_buffer
        self.turn_buffer = ""
        self.in_turn = False
        ts = self.clock.now()
        self.last_commit_ts = ts
        self.last_committed_text = text

        parse = self.nlu.parse_turn(text)
        intent = parse.get("intent")
        corrected = bool(parse.get("corrected"))
        # Override-style turns ("actually make that Tokyo", "instead do X...")
        # or pure slot-fill fragments ("from Berlin", "two passengers")
        # carry intent by reference to the session's active task.
        if not intent and self.snapshot.intent:
            tl = text.lower()
            carries_slots = bool(parse.get("slots"))
            if any(w in tl for w in ("that", "it", "instead", "actually",
                                     "rather", "correction", "wrong", "change")) \
                    or carries_slots:
                parse["intent"] = self.snapshot.intent
                intent = self.snapshot.intent
                corrected = True
        self.snapshot.apply_turn(intent, parse.get("slots", {}),
                                 self.turn_no, ts, corrected=corrected)

        plan = self.planner.build(text, parse, self.snapshot, self.session)
        if not plan.steps:
            await self._handle_no_steps(plan.note, intent, text, parse)
            return

        # fast-path narration — substantive, truthful, immediate
        narration = self.brain.narrate_intent(intent, self.snapshot.slots, text)
        if narration and self._spoken_throttle.allow(self.clock, "narrate"):
            await self._emit(ACTION_NARRATION, {"text": narration})
        elif narration:
            # rapid follow-up: the throttle just fired, but the spoken surface
            # must stay alive during storms — a short informed ack beats silence
            # until the final response lands.
            await self._emit(ACTION_FILLER,
                             {"text": self.brain.narrate_interrupt()})

        # supersede an old in-flight plan whose first step is incompatible
        new_fp = self._plan_signature(plan)
        self._maybe_invalidate(new_fp)

        epoch = self.epoch
        task = asyncio.create_task(self._run_plan(plan, epoch, self.turn_no))
        self.tasks.append(task)

    def _plan_signature(self, plan: Plan) -> str:
        if not plan.steps:
            return "none"
        st = plan.steps[0]
        spec = self.registry.get(st.tool)
        try:
            args, _ = self.planner.map_args(spec, dict(self.snapshot.slots),
                                            self.last_committed_text)
        except Exception:
            args = {}
        if not args:
            return st.tool
        return fingerprint(st.tool, args)

    def _maybe_invalidate(self, new_fp: str) -> None:
        active = self.active
        if not active or not active.get("plan"):
            return
        if new_fp == active.get("fp"):
            return  # same landing zone: keep in-flight work (reuse!)
        if active.get("epoch", self.epoch) < self.epoch:
            return
        self.epoch += 1
        self._cancel_inflight(older_than=self.epoch)
        if active.get("task"):
            active["task"].cancel()
        self.active = None

    async def _handle_no_steps(self, note: str, intent: Optional[str],
                               text: str, parse: Dict[str, Any]) -> None:
        t = (text or "").lower()
        if re.search(r"\b(hi|hello|hey|good\s*(morning|afternoon|evening))\b", t) \
                and len(t) < 40:
            await self._emit_response("Hi there — how can I help you today?", None)
            return
        if re.search(r"\b(thanks|thank you|cheers|perfect|awesome|great)\b", t) \
                and len(t) < 40:
            await self._emit_response("You're welcome!", None)
            return
        if intent is None:
            await self._emit_clarify_intent(
                "I didn't catch that — what would you like me to do?")
            return
        await self._emit_clarify_intent(
            "That's outside what I can do right now — happy to help with "
            "flights, bookings, tickets, or manuals.")

    # ==================================================================
    # Slow path: plan execution
    # ==================================================================
    async def _run_plan(self, plan: Plan, epoch: int, turn_no: int) -> None:
        self.active = {"plan": plan, "epoch": epoch, "fp": self._plan_signature(plan),
                       "task": asyncio.current_task()}
        results: Dict[int, Any] = {}
        issued: set = set()
        relaxed: set = set()
        last_progress = self.clock.now()
        try:
            while not self.stop:
                if self.epoch != epoch:
                    return  # superseded by interrupt / newer turn

                # (0) empty search -> relax the date, re-run before downstream steps
                if self.cfg.relax_empty_search:
                    for s in list(issued):
                        step = self._step_by_seq(plan, s)
                        if s in relaxed or step is None or not self.planner._is_search(step.tool):
                            continue
                        outcome = results.get(s)
                        rec = outcome.get("rec") if isinstance(outcome, dict) else None
                        empty = (outcome.get("val")
                                 if "val" in (outcome or {}) else None)
                        if empty is None:
                            empty = rec.result if rec and rec.done_event.is_set() else None
                        if isinstance(empty, dict) and self._search_empty(empty) \
                                and rec and rec.ok:
                            relaxed.add(s)
                            new_args = dict(rec.args)
                            new_args.pop("date", None)
                            await self._emit(ACTION_NARRATION, {
                                "text": "No flights that day — looking at nearby dates."})
                            new_rec = await self._reissue_readonly(step, rec, new_args, epoch)
                            results[s] = {"rec": new_rec}

                # (a) issue eligible, unissued steps
                for step in plan.steps:
                    if step.seq in issued:
                        continue
                    if not all(self._dep_resolved(results, d) for d in step.deps):
                        continue
                    outcome = await self._issue_step(step, epoch, results)
                    issued.add(step.seq)
                    if outcome is CLARIFY:
                        return
                    results[step.seq] = outcome

                # (b) retry failed read-only calls (legitimate, not a stale rerun)
                for s in list(issued):
                    step = self._step_by_seq(plan, s)
                    outcome = results.get(s)
                    rec = outcome.get("rec") if isinstance(outcome, dict) else None
                    if rec and rec.done_event.is_set() and not rec.ok \
                            and not rec.state_modifying \
                            and rec.retries < self.cfg.max_retries:
                        rec.retries += 1
                        new_rec = await self._emit_retry(step, rec, epoch)
                        results[s] = {"rec": new_rec}

                # (c) all steps issued & resolved -> finalize
                if issued and all(s in results and self._resolved(results[s])
                                  for s in issued) \
                        and all(x.seq in issued for x in plan.steps):
                    await self._finalize(plan, results, epoch)
                    return

                # (d) wait for next tool_result, progress-narrate meanwhile
                self.wake.clear()
                got = True
                try:
                    got = await asyncio.wait_for(
                        self.wake.wait(),
                        timeout=max(0.01, self.cfg.progress_filler_ms * 0.001))
                except asyncio.TimeoutError:
                    got = False
                if not got:
                    elapsed = self.clock.now() - last_progress
                    if elapsed > self.cfg.long_task_filler_ms * 0.001 \
                            and self._spoken_throttle.allow(self.clock, "long"):
                        await self._emit(ACTION_FILLER,
                                         {"text": self.brain.filler("long")})
                    elif self._spoken_throttle.allow(self.clock, "progress"):
                        await self._emit(ACTION_FILLER,
                                         {"text": self.brain.filler("progress")})
        except asyncio.CancelledError:
            return
        finally:
            if self.active and self.active.get("task") is asyncio.current_task():
                self.active = None

    @staticmethod
    def _step_by_seq(plan: Plan, seq: int) -> Optional[Step]:
        for st in plan.steps:
            if st.seq == seq:
                return st
        return None

    @staticmethod
    def _resolved(outcome: Any) -> bool:
        if not isinstance(outcome, dict):
            return False
        if "val" in outcome:
            return True
        rec = outcome.get("rec")
        return bool(rec and rec.done_event.is_set())

    @staticmethod
    def _dep_resolved(results: Dict[int, Any], seq: int) -> bool:
        return Orchestrator._resolved(results.get(seq))

    @staticmethod
    def _payload_view(results: Dict[int, Any]) -> Dict[int, Any]:
        """Expose a dependency's real tool payload (not the call record)
        to downstream arg builders."""
        view: Dict[int, Any] = {}
        for k, v in results.items():
            if "val" in v:
                view[k] = v["val"]
            elif v.get("rec") is not None:
                rec = v["rec"]
                view[k] = rec.result if rec.done_event.is_set() else v
        return view

    async def _emit_retry(self, step: Optional[Step], old: CallRecord,
                          epoch: int) -> CallRecord:
        call_id = self.registry.next_call_id(old.tool)
        rec = CallRecord(call_id, old.tool, old.args, old.fp, epoch,
                         speculative=False, state_modifying=old.state_modifying,
                         clock=self.clock)
        self.recs[call_id] = rec
        self._index_fp(rec)
        await self._emit_tool_call(call_id, old.tool, old.args, speculative=False)
        return rec

    @staticmethod
    def _search_empty(payload: Any) -> bool:
        """True when a search/availability call returned zero results."""
        data = payload
        if isinstance(data, dict):
            if "count" in data and data["count"] == 0:
                return True
            for k in ("flights", "results", "options", "items"):
                if isinstance(data.get(k), list) and not data[k]:
                    return True
        return False

    async def _reissue_readonly(self, step: Step, old: CallRecord,
                                new_args: Dict[str, Any], epoch: int) -> CallRecord:
        call_id = self.registry.next_call_id(old.tool)
        fp = fingerprint(step.tool, new_args)
        rec = CallRecord(call_id, step.tool, new_args, fp, epoch,
                         speculative=False, state_modifying=False, clock=self.clock)
        self.recs[call_id] = rec
        self._index_fp(rec)
        await self._emit_tool_call(call_id, step.tool, new_args, speculative=False)
        return rec

    async def _issue_step(self, step: Step, epoch: int,
                          results: Dict[int, Any]) -> Any:
        args = step.build(self._payload_view(results))
        if args is MISSING or args is None:
            slots = dict(self.snapshot.slots)
            missing = self.planner.missing_params(step.tool, slots,
                                                  self._payload_view(results))
            await self._emit_clarify(step.tool, missing, slots)
            return CLARIFY
        if not isinstance(args, dict):
            args = {}
        args = self._enrich_args(step.tool, args)

        spec = self.registry.get(step.tool)
        sm = bool(spec and spec.state_modifying)
        fp = fingerprint(step.tool, args)

        # 1) state-modifying: never duplicate; reuse completed work; reuse live
        if sm:
            decision = self.ledger.resolve_issue(step.tool, args)
            if decision["kind"] == "done":
                return {"val": decision["result"]}
            if decision["kind"] == "inflight":
                for cid in decision["call_ids"]:
                    rec = self.recs.get(cid)
                    if rec and not rec.cancelled and not rec.done_event.is_set():
                        rec.speculative = False
                        return {"rec": rec}
                # all prior records dead/cancelled -> safe to issue fresh

        # 2) read-only reuse: completed spec result, or live matching call
        live_id = self._fp_index.get(fp)
        live = self.recs.get(live_id) if live_id else None
        if not sm and fp in self.spec_cache:
            return {"val": self.spec_cache[fp]}
        if live is not None and not live.cancelled and live.epoch == epoch:
            live.speculative = False
            return {"rec": live}
        if live is not None and not live.cancelled and sm is False \
                and live.done_event.is_set() and live.ok:
            # cross-epoch reuse of a completed read-only call: guard against
            # resurrecting a stale EMPTY search payload after an interrupt.
            if not (self.planner._is_search(step.tool)
                    and self._search_empty(live.result)):
                return {"val": live.result}

        call_id = self.registry.next_call_id(step.tool)
        rec = CallRecord(call_id, step.tool, args, fp, epoch,
                         speculative=False, state_modifying=sm, clock=self.clock)
        self.recs[call_id] = rec
        self._index_fp(rec)
        if sm:
            self.ledger.mark_issued(step.tool, args, call_id, fp)
        await self._emit_tool_call(call_id, step.tool, args, speculative=False)
        if self.planner._is_search(step.tool):
            self.session.last_search = dict(args)
        if not sm and step.tool not in ("flight_search", "book_flight"):
            for k, v in args.items():
                if v is not None and k not in ("frame_ref", "frame"):
                    self.snapshot.set_result(k, v)
        return {"rec": rec}

    async def _finalize(self, plan: Plan, results: Dict[int, Any], epoch: int) -> None:
        # apply transforms in step order so the chain's state lands in snapshot
        for step in plan.steps:
            outcome = results.get(step.seq)
            if outcome is None or not isinstance(outcome, dict):
                continue
            val = outcome.get("val")
            if val is None and outcome.get("rec"):
                rec: CallRecord = outcome["rec"]
                val = rec.result if rec.ok else None
            if step.on_result and val is not None:
                step.on_result(val, self.snapshot, self.session)

        last = plan.steps[-1]
        outcome = results.get(last.seq)
        last_tool = last.tool
        result = None
        ok = True
        if isinstance(outcome, dict):
            rec = outcome.get("rec")
            if "val" in outcome:
                result = outcome["val"]
            elif rec is not None:
                result = rec.result if rec.ok else None
                ok = rec.ok
        self.snapshot.set_meta("last_tool", last_tool)
        text = self.brain.respond(result, self.snapshot.to_dict(), last_tool)
        snap = self.snapshot.to_dict()
        snap["result_ok"] = bool(ok)
        await self._emit_response(text, snap)

    # ==================================================================
    # Tool result handling
    # ==================================================================
    async def _on_tool_result(self, ev: Event) -> None:
        d = ev.data
        call_id = str(d.get("call_id") or d.get("callId") or d.get("id") or "")
        if not call_id:
            return
        rec = self.recs.get(call_id)
        if rec is None:
            return  # late result for a call we long forgot: ignore safely
        ok = self._result_ok(d)
        result = self._result_payload(d)
        rec.ok = ok
        rec.result = result
        rec.error = d.get("error") or (None if ok else d.get("result"))
        rec.done_event.set()
        rec.alive = False

        if ok:
            if rec.state_modifying:
                self.ledger.mark_completed(rec.tool, rec.args, result, rec.fp)
            else:
                # never let an EMPTY search result be treated as final:
                # a re-plan on the same query must re-search so date
                # relaxation can still rescue it (interrupt race case).
                if not (self.planner._is_search(rec.tool)
                        and self._search_empty(result)):
                    self.spec_cache[rec.fp] = result
            if self.planner._is_search(rec.tool):
                flights = self.planner._extract_flights(result)
                if flights:
                    self.session.last_flights = flights
        elif rec.state_modifying:
            self.ledger.mark_failed(rec.tool, rec.args, rec.fp)
        self.wake.set()

    @staticmethod
    def _result_ok(d: Dict[str, Any]) -> bool:
        if "ok" in d:
            return bool(d["ok"])
        if "success" in d:
            return bool(d["success"])
        if d.get("error"):
            return False
        if isinstance(d.get("result"), dict) and d["result"].get("ok") is False:
            return False
        return True

    @staticmethod
    def _result_payload(d: Dict[str, Any]) -> Any:
        if "result" in d:
            return d["result"]
        if "data" in d:
            return d["data"]
        out = {k: v for k, v in d.items()
               if k not in ("call_id", "callId", "id", "ts", "type", "ok",
                            "success", "error")}
        return out if out else {"ok": True}

    # ==================================================================
    # Manifest / multimodal / interrupt
    # ==================================================================
    def _on_manifest(self, ev: Event) -> None:
        payload = ev.data
        if isinstance(payload, list):
            payload = {"tools": payload}
        if not isinstance(payload, dict):
            payload = {}
        tools = payload.get("tools") or []
        if isinstance(tools, list):
            self.registry.load_manifest(payload)
            self.nlu.load_manifest(tools)
        ref = payload.get("reference_date") or payload.get("today") or payload.get("date")
        if ref:
            self.begin_scenario(ref)

    async def _on_audio(self, ev: Event) -> None:
        if self._ack_throttle.allow(self.clock, "audio"):
            await self._emit(ACTION_FILLER, {"text": "Mm-hmm, got it."})
        task = asyncio.create_task(self._ground_audio(ev))
        self.tasks.append(task)

    async def _ground_audio(self, ev: Event) -> None:
        info = await self.grounding.ingest_audio(
            ev.data.get("wav") or ev.data.get("audio") or ev.data.get("data"))
        transcript = info.get("transcript")
        if transcript:
            gap = (self.clock.now() - self.last_text_event_ts) > 1.2
            if gap or self.last_text_event_ts < 0:
                await self._on_transcript(Event(EVENT_TRANSCRIPT, {
                    "text": transcript, "final": True, "end_of_turn": True},
                    ts=ev.ts))

    async def _on_frame(self, ev: Event) -> None:
        info = await self.grounding.ingest_frame(
            ev.data.get("png") or ev.data.get("frame") or ev.data.get("data"))
        if info.get("ok"):
            self.session.last_frame_ref = info.get("frame_ref")
            self.session.last_frame_desc = info.get("description")
            self.snapshot.set_meta("last_frame_ref", info.get("frame_ref"))
            if self._spoken_throttle.allow(self.clock, "frame"):
                await self._emit(ACTION_FILLER,
                                 {"text": "Thanks — I can see the device now."})
        else:
            await self._emit(ACTION_FILLER,
                             {"text": "Hmm — the camera feed didn't come through clearly."})
        self.wake.set()

    async def _on_interrupt(self, ev: Event) -> None:
        self.epoch += 1
        await self.clock.sleep(self.cfg.interrupt_ack_ms * 0.001)
        if self._spoken_throttle.allow(self.clock, "interrupt"):
            await self._emit(ACTION_FILLER,
                             {"text": self.brain.narrate_interrupt()})
        self._cancel_inflight(older_than=self.epoch)
        if self.active and self.active.get("task"):
            self.active["task"].cancel()
        self.active = None

    def _cancel_inflight(self, older_than: int) -> None:
        doomed = [r for r in self.recs.values()
                  if r.alive and not r.cancelled and not r.done_event.is_set()
                  and r.epoch < older_than]
        for rec in doomed:
            t = asyncio.create_task(self._grace_cancel(rec))
            self.outstanding_cancel_tasks.append(t)

    async def _grace_cancel(self, rec: CallRecord) -> None:
        try:
            await self.clock.sleep(self.cfg.cancel_grace_ms * 0.001)
            if rec.done_event.is_set() or rec.cancelled or self.stop:
                return
            rec.cancelled = True
            await self._emit_cancel([rec.call_id])
        except asyncio.CancelledError:
            pass

    # ==================================================================
    # Enrichment / clarifications
    # ==================================================================
    def _enrich_args(self, tool: str, args: Dict[str, Any]) -> Dict[str, Any]:
        spec = self.registry.get(tool)
        if spec is None:
            return args
        out = dict(args)
        wants_frame = any(
            any(w in (p.get("name") or "").lower() for w in ("frame", "image", "photo"))
            for p in spec.params)
        if wants_frame and not any(k in out for k in ("frame", "frame_ref", "image")):
            if self.session.last_frame_ref:
                out["frame_ref"] = self.session.last_frame_ref
        for k, v in list(out.items()):
            if isinstance(v, str) and v.startswith("$") and v[1:] in out:
                out[k] = out[v[1:]]
        return out

    async def _emit_clarify(self, tool: str, missing: List[Dict[str, Any]],
                            slots: Dict[str, Any]) -> None:
        if not missing:
            await self._emit_clarify_intent("Could you give me a bit more detail?")
            return
        primary = missing[0]
        name = primary.get("name") or "value"
        self.snapshot.mark_uncertain([name])
        question = self.brain.clarify([primary])
        data: Dict[str, Any] = {"question": question, "expected": [name],
                                "snapshot": self.snapshot.to_dict()}
        if self.cfg.clarify_limit > 1:
            data["all_missing"] = [m.get("name") for m in missing[:self.cfg.clarify_limit]]
        await self._emit(ACTION_CLARIFY, data)

    async def _emit_clarify_intent(self, question: str) -> None:
        self.snapshot.mark_uncertain(["intent"])
        await self._emit(ACTION_CLARIFY, {
            "question": question,
            "expected": ["intent"],
            "snapshot": self.snapshot.to_dict(),
        })

    # ==================================================================
    # Emission
    # ==================================================================
    async def _emit(self, dtype: str, data: Dict[str, Any]) -> None:
        if self.stop:
            return
        action = Action(dtype, data, ts=self.clock.now())
        self.trace.append(action.to_dict())
        await self._send(action.to_dict())

    async def _emit_tool_call(self, call_id: str, tool: str,
                              args: Dict[str, Any], speculative: bool) -> None:
        data: Dict[str, Any] = {"call_id": call_id, "tool": tool, "args": args}
        if speculative:
            data["speculative"] = True
        await self._emit("tool_call", data)

    async def _emit_cancel(self, call_ids: List[str]) -> None:
        await self._emit("cancel", {"call_ids": call_ids})

    async def _emit_response(self, text: str, snap: Optional[Dict[str, Any]]) -> None:
        data: Dict[str, Any] = {"text": text}
        if snap is not None:
            data["snapshot"] = snap
        await self._emit(ACTION_RESPONSE, data)

    def _index_fp(self, rec: CallRecord) -> None:
        self._fp_index[rec.fp] = rec.call_id

    def _end(self, ev: Event) -> None:
        self.stop = True
        for t in self.tasks:
            t.cancel()
        self.tasks.clear()
        for t in self.outstanding_cancel_tasks:
            t.cancel()
        self.outstanding_cancel_tasks.clear()