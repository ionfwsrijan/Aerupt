import { useEffect, useMemo, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { Ban, Sparkle, Volume2 } from "lucide-react";
import type { FeedState } from "../lib/feed";
import type { StampedMessage, ToolItem } from "../lib/types";
import { ToolCard } from "./ToolCard";

function TypeWriter({ text, onDone }: { text: string; onDone?: () => void }) {
  const [n, setN] = useState(0);
  const doneRef = useRef(false);
  useEffect(() => {
    setN(0);
    doneRef.current = false;
    const iv = window.setInterval(() => {
      setN((v) => {
        if (v >= text.length) {
          if (!doneRef.current) {
            doneRef.current = true;
            onDone?.();
          }
          return v;
        }
        return v + 2;
      });
    }, 16);
    return () => window.clearInterval(iv);
    // onDone intentionally not in deps (stable via ref usage)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [text]);
  return (
    <span>
      {text.slice(0, n)}
      {n < text.length && <span className="typing-caret text-cyan-300">▍</span>}
    </span>
  );
}

function Clarify({ data, onReply }: { data: Record<string, unknown>; onReply: (t: string) => void }) {
  const q = String(data.question ?? "");
  const expected = (data.expected as string[]) ?? [];
  const opts = (data.options as string[]) ?? [];
  const quick = opts.length ? opts : expected.map((e) => (e === "intent" ? "No, actually…" : e));
  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      className="max-w-[480px] rounded-2xl rounded-tl-sm border border-violet-400/20 bg-violet-500/[0.07] px-4 py-3"
    >
      <div className="mb-2 text-[14px] leading-relaxed text-violet-50">{q}</div>
      <div className="flex flex-wrap gap-1.5">
        {quick.slice(0, 4).map((o) => (
          <button
            key={o}
            onClick={() => onReply(o)}
            className="rounded-full border border-white/15 bg-white/[0.06] px-2.5 py-1 text-[11px] font-medium text-white/105 transition hover:border-violet-300/40 hover:bg-violet-500/20"
          >
            {o}
          </button>
        ))}
      </div>
    </motion.div>
  );
}

function userBubble(m: StampedMessage) {
  return (
    <motion.div
      layout
      initial={{ opacity: 0, x: 12 }}
      animate={{ opacity: 1, x: 0 }}
      className="flex justify-end"
    >
      <div className="max-w-[70%] rounded-2xl rounded-tr-sm bg-indigo-500/90 px-4 py-2.5 text-[14px] leading-relaxed text-white shadow-lg shadow-indigo-950/40">
        {String(m.data.text ?? "")}
      </div>
    </motion.div>
  );
}

export function ChatFeed({
  feed,
  onReply,
  suggestions,
}: {
  feed: FeedState;
  onReply: (t: string) => void;
  suggestions?: string[];
}) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const items = feed.items;

  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
  }, [items.length]);

  const blocks = useMemo(() => items.map((m) => ({ m })), [items]);

  return (
    <div ref={scrollRef} className="h-full overflow-y-auto px-6 py-6">
      <div className="mx-auto flex max-w-3xl flex-col gap-3">
        <AnimatePresence initial={false}>
          {blocks.length === 0 && (
            <motion.div
              initial={{ opacity: 0, y: 10 }}
              animate={{ opacity: 1, y: 0 }}
              className="mt-2 text-center"
            >
              <h2 className="text-lg font-semibold text-white">Talk to AERUPT.</h2>
              <p className="mx-auto mt-1.5 max-w-md text-[13px] leading-relaxed text-off">
                Say something like{" "}
                <span className="text-white/100">“Book a flight from Paris to London tomorrow.”</span>{" "}
                then interrupt with{" "}
                <span className="text-white/100">“Actually, to Tokyo.”</span> and watch AERUPT
                gracefully cancel and redirect.
              </p>
              {suggestions && suggestions.length > 0 && (
                <div className="mx-auto mt-4 flex max-w-xl flex-wrap justify-center gap-1.5">
                  {suggestions.map((s) => (
                    <button
                      key={s}
                      onClick={() => onReply(s)}
                      className="rounded-full border border-white/15 bg-white/[0.06] px-3 py-1.5 text-[12px] font-medium text-off transition hover:border-cyan-300/40 hover:bg-cyan-500/15 hover:text-white"
                    >
                      {s}
                    </button>
                  ))}
                </div>
              )}
            </motion.div>
          )}

          {blocks.map(({ m }) => {
            if (m.kind === "user") return userBubble(m);
            if (m.kind === "tool_call") {
              return <ToolCard key={m.id} tool={m as ToolItem} />;
            }
            if (m.kind === "filler") {
              return (
                <motion.div
                  key={m.id}
                  layout
                  initial={{ opacity: 0 }}
                  animate={{ opacity: 1 }}
                  className="flex items-center gap-2 self-center"
                >
                  <span className="flex gap-1">
                    <span className="h-1 w-1 animate-bounce rounded-full bg-indigo-300/70 [animation-delay:0ms]" />
                    <span className="h-1 w-1 animate-bounce rounded-full bg-indigo-300/70 [animation-delay:120ms]" />
                    <span className="h-1 w-1 animate-bounce rounded-full bg-indigo-300/70 [animation-delay:240ms]" />
                  </span>
                  <span className="text-[12px] italic text-off">
                    {String(m.data.text ?? "")}
                  </span>
                </motion.div>
              );
            }
            if (m.kind === "cancel") {
              const cids = (m.data.call_ids as string[]) ?? [];
              return (
                <motion.div
                  key={m.id}
                  layout
                  initial={{ opacity: 0, y: 6 }}
                  animate={{ opacity: 1, y: 0 }}
                  className="flex items-center gap-2 self-center rounded-full border border-rose-500/25 bg-rose-500/[0.08] px-3 py-1"
                >
                  <Ban className="h-3 w-3 text-rose-400" />
                  <span className="text-[11px] font-medium text-rose-200">
                    interrupt — grace-cancelled {cids.length} in-flight call{cids.length === 1 ? "" : "s"}
                  </span>
                </motion.div>
              );
            }
            if (m.kind === "clarify") {
              return <Clarify key={m.id} data={m.data} onReply={onReply} />;
            }
            if (m.kind === "narration") {
              const text = String(m.data.text ?? "");
              return (
                <motion.div layout initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }}>
                  <div className="mb-1 flex items-center gap-1.5 pl-1">
                    <span className="inline-flex items-center gap-1 text-[10px] font-semibold uppercase tracking-widest text-cyan-300">
                      <Volume2 className="h-3 w-3" /> AERUPT
                    </span>
                  </div>
                  <div className="max-w-[85%] rounded-2xl rounded-tl-sm border border-white/10 bg-white/[0.05] px-4 py-2.5 text-[14px] leading-relaxed text-white/95">
                    <TypeWriter key={m.id} text={text} />
                  </div>
                </motion.div>
              );
            }
            if (m.kind === "response") {
              const text = String(m.data.text ?? "");
              return (
                <motion.div
                  layout
                  initial={{ opacity: 0, y: 10 }}
                  animate={{ opacity: 1, y: 0 }}
                  className="relative"
                >
                  <div className="absolute -inset-px rounded-2xl bg-gradient-to-r from-indigo-500/40 via-violet-500/30 to-cyan-400/40 blur-[1px]" />
                  <div className="relative rounded-2xl border border-white/10 bg-[#0b0e17]/90 px-5 py-3.5 backdrop-blur-xl">
                    <div className="mb-1.5 flex items-center gap-1.5">
                      <Sparkle className="h-3.5 w-3.5 text-violet-300" />
                      <span className="text-[10px] font-semibold uppercase tracking-widest text-violet-300">
                        committed
                      </span>
                    </div>
                    <div className="text-[15px] font-medium leading-relaxed text-white">
                      {text}
                    </div>
                  </div>
                </motion.div>
              );
            }
            return null;
          })}
        </AnimatePresence>
      </div>
    </div>
  );
}