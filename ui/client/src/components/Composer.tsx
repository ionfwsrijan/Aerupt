import { useEffect, useRef, useState } from "react";
import { Ban, Mic, MicOff, Send, Square, Zap } from "lucide-react";
import { useSpeech } from "../hooks/useSpeech";
import type { ConnState } from "../lib/types";

const SUGGESTIONS = [
  "Book a flight from Paris to London tomorrow.",
  "Book it for two people in business.",
  "Actually, to Tokyo instead.",
];

export function Composer({
  conn,
  onCommit,
  onPartial,
  onInterrupt,
  onMicState,
}: {
  conn: ConnState;
  onCommit: (t: string) => void;
  onPartial: (t: string) => void;
  onInterrupt: (r: string) => void;
  onMicState?: (active: boolean) => void;
}) {
  const [text, setText] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);
  const debounceRef = useRef<number | null>(null);
  const { state, interim, supported, toggle } = useSpeech(onPartial, onCommit);
  const micOn = state === "busy" || state === "listening";

  useEffect(() => {
    onMicState?.(micOn);
  }, [micOn, onMicState]);

  const flushPartial = () => {
    if (debounceRef.current !== null) {
      window.clearTimeout(debounceRef.current);
      debounceRef.current = null;
    }
  };

  useEffect(() => flushPartial, []);

  const onChange = (v: string) => {
    setText(v);
    flushPartial();
    if (v.trim().length >= 3) {
      debounceRef.current = window.setTimeout(() => onPartial(v.trim()), 380);
    }
  };

  const submit = () => {
    const t = text.trim();
    if (!t) return;
    flushPartial();
    onCommit(t);
    setText("");
    inputRef.current?.focus();
  };

  const quick = (q: string) => {
    onCommit(q);
  };

  const barge = () => {
    onInterrupt("user_override");
  };

  const micTitle = !supported
    ? "Voice input isn't available in this browser — try Chrome or Edge"
    : micOn
      ? "Stop and send what you said"
      : "Speak — AERUPT transcribes live and starts working before you finish";

  return (
    <div className="relative z-10 border-t border-white/10 px-6 pb-5 pt-4">
      <div className="mx-auto max-w-3xl">
        <div className="mb-2.5 flex flex-wrap gap-1.5">
          {SUGGESTIONS.map((s) => (
            <button
              key={s}
              onClick={() => quick(s)}
              className="rounded-full border border-white/10 bg-white/[0.04] px-2.5 py-1 text-[11px] text-off transition hover:border-indigo-300/30 hover:text-white active:scale-95"
            >
              {s}
            </button>
          ))}
        </div>
        <div className="flex items-center gap-2 rounded-2xl border border-white/10 bg-white/[0.04] p-2 backdrop-blur-xl focus-within:border-indigo-400/40">
          <input
            ref={inputRef}
            value={text}
            onChange={(e) => onChange(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                submit();
              }
              if (e.key === "Escape") {
                flushPartial();
                setText("");
              }
            }}
            placeholder={
              micOn ? "Listening…" : "Type, or press the mic to speak — AERUPT works while you talk…"
            }
            className="min-w-0 flex-1 bg-transparent px-3 text-[14px] text-white placeholder:text-off/70 focus:outline-none"
            autoFocus
          />
          <button
            onClick={toggle}
            disabled={!supported || conn !== "open"}
            title={micTitle}
            className={`relative inline-flex items-center gap-1.5 rounded-xl border px-3 py-2 text-xs font-semibold transition active:scale-95 disabled:cursor-not-allowed disabled:opacity-40 ${
              micOn
                ? "border-rose-400/40 bg-rose-500/15 text-rose-200"
                : "border-white/10 bg-white/[0.04] text-off hover:border-cyan-300/40 hover:text-cyan-200"
            }`}
          >
            {micOn && (
              <span className="absolute inset-0 -z-10 animate-ping rounded-xl border border-rose-400/40" />
            )}
            {micOn ? <Square className="h-3.5 w-3.5" /> : <Mic className="h-3.5 w-3.5" />}
            {micOn ? "Stop" : "Mic"}
          </button>
          <button
            onClick={barge}
            disabled={conn !== "open"}
            title="Interrupt AERUPT while it is acting (30 ms grace-cancel)"
            className="inline-flex items-center gap-1.5 rounded-xl border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-xs font-semibold text-rose-300 transition hover:bg-rose-500/20 active:scale-95 disabled:opacity-40"
          >
            <Ban className="h-3.5 w-3.5" />
            Interrupt
          </button>
          <button
            onClick={submit}
            disabled={conn !== "open" || !text.trim()}
            title="Send"
            className="btn-primary !px-3.5 !py-2 disabled:cursor-not-allowed"
          >
            <Send className="h-4 w-4" />
          </button>
        </div>

        <div className="mt-2 flex min-h-[18px] items-center justify-between gap-3 px-1">
          {micOn && interim ? (
            <span className="flex items-center gap-2 text-[12px] text-cyan-200/90">
              <Mic className="h-3.5 w-3.5 animate-pulse text-rose-300" />
              <span>“{interim}”</span>
            </span>
          ) : state === "error" ? (
            <span className="flex items-center gap-2 text-[12px] text-rose-300/90">
              <MicOff className="h-3.5 w-3.5" />
              Mic blocked — allow microphone access and press Mic again.
            </span>
          ) : state === "unsupported" ? (
            <span className="flex items-center gap-2 text-[11px] text-off/70">
              <MicOff className="h-3 w-3" />
              Voice input needs Chrome or Edge — typing works everywhere.
            </span>
          ) : null}
          <span className="ml-auto flex shrink-0 items-center gap-1.5 pl-3 text-[11px] text-off/80">
            <Zap className="h-3 w-3 text-cyan-300" />
            speculation fires on partials — Enter commits, Interrupt barges in.
          </span>
        </div>
      </div>
    </div>
  );
}