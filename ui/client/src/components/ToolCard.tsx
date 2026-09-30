import { motion } from "framer-motion";
import { Ban, Check, Loader2, Sparkles, X } from "lucide-react";
import type { ToolItem } from "../lib/types";

const ICONS: Record<string, string> = {
  flight_search: "✈",
  book_flight: "🎟",
  cancel_booking: "↩",
  create_ticket: "🎫",
  lookup_manual: "📖",
  rent_car: "🚗",
};

export function ToolCard({ tool }: { tool: ToolItem }) {
  const { tool: name, args, speculative, status, summary, error } = tool;
  const resolved = status !== "running";
  const ok = status === "done";

  return (
    <motion.div
      layout
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      className="group max-w-[440px]"
    >
      <div
        className={`overflow-hidden rounded-xl border backdrop-blur-md transition ${
          status === "cancelled"
            ? "border-rose-500/25 bg-rose-500/[0.06]"
            : status === "error"
              ? "border-amber-500/25 bg-amber-500/[0.06]"
              : ok
                ? "border-emerald-400/20 bg-emerald-400/[0.05]"
                : "border-white/10 bg-white/[0.04]"
        }`}
      >
        <div className="flex items-center gap-2.5 px-3 py-2">
          <span className="grid h-6 w-6 shrink-0 place-items-center rounded-lg bg-white/[0.06] text-[13px]">
            {ICONS[name] ?? "⚙"}
          </span>
          <span className="font-mono text-[12px] font-semibold text-white/90">{name}</span>
          {speculative && (
            <span className="inline-flex items-center gap-1 rounded-full border border-cyan-400/20 bg-cyan-400/10 px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wider text-cyan-300">
              <Sparkles className="h-2.5 w-2.5" /> speculate
            </span>
          )}
          <span className="ml-auto inline-flex items-center gap-1.5">
            {status === "running" && (
              <>
                <Loader2 className="spin-slow h-3.5 w-3.5 text-indigo-300" />
                <span className="text-[10px] font-medium uppercase tracking-wider text-indigo-200/80">
                  running
                </span>
              </>
            )}
            {status === "done" && (
              <span className="inline-flex items-center gap-1 text-[10px] font-semibold uppercase tracking-wider text-emerald-400">
                <Check className="h-3.5 w-3.5" /> done
              </span>
            )}
            {status === "cancelled" && (
              <span className="inline-flex items-center gap-1 text-[10px] font-semibold uppercase tracking-wider text-rose-400">
                <Ban className="h-3.5 w-3.5" /> cancelled
              </span>
            )}
            {status === "error" && (
              <span className="inline-flex items-center gap-1 text-[10px] font-semibold uppercase tracking-wider text-amber-400">
                <X className="h-3.5 w-3.5" /> failed
              </span>
            )}
          </span>
        </div>
        <div className="flex flex-wrap gap-1 px-3 pb-2">
          {Object.entries(args).map(([k, v]) => (
            <span key={k} className="chip font-mono !text-[10px]">
              <span className="text-off">{k}</span>
              <span className="text-white/90">{String(v)}</span>
            </span>
          ))}
          {summary && resolved && (
            <span className="ml-auto truncate font-mono text-[10px] text-emerald-200/80">
              {summary}
            </span>
          )}
        </div>
        {error && (
          <div className="border-t border-white/5 px-3 py-1.5 font-mono text-[10px] text-amber-300/90">
            {error}
          </div>
        )}
      </div>
    </motion.div>
  );
}