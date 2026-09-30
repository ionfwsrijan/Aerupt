import { useEffect, useState } from "react";
import { motion } from "framer-motion";
import { Ban, Gauge, Layers, Target } from "lucide-react";
import type { FeedState } from "../lib/feed";
import { flattenSlots, latestSnapshot, toolCount } from "../lib/feed";

function SlotChips({ slots }: { slots: Array<{ key: string; value: string }> }) {
  if (!slots.length) {
    return <p className="text-[11px] text-off/70">no slots parsed yet</p>;
  }
  return (
    <div className="mt-1 flex flex-wrap gap-1.5">
      {slots.map((s) => (
        <span key={s.key} className="chip">
          <span className="text-indigo-300/80">{s.key}:</span>
          <span className="text-white/90">{s.value}</span>
        </span>
      ))}
    </div>
  );
}

export function SnapshotPanel({ feed }: { feed: FeedState }) {
  const snap = latestSnapshot(feed);
  const counts = toolCount(feed);
  const slotList = snap ? flattenSlots(snap.slots) : [];

  return (
    <div className="glass p-4">
      <div className="mb-3 flex items-center gap-2">
        <Layers className="h-4 w-4 text-violet-300" />
        <h3 className="text-[12px] font-semibold uppercase tracking-widest text-off">
          Session snapshot
        </h3>
      </div>

      <div className="mb-3 flex items-center gap-2">
        <Target className="h-4 w-4 text-indigo-300" />
        <div>
          <div className="text-[10px] uppercase tracking-widest text-off">intent</div>
          <div className="font-mono text-[13px] font-semibold text-white">
            {snap?.intent || "—"}
          </div>
        </div>
      </div>

      <div className="mb-3">
        <div className="mb-1 text-[10px] uppercase tracking-widest text-off">slots</div>
        <SlotChips slots={slotList} />
      </div>

      <div className="mb-3 grid grid-cols-3 gap-2">
        <Metric label="calls" value={counts.calls} tone="text-white" />
        <Metric label="resolved" value={counts.resolved} tone="text-emerald-300" />
        <Metric label="cancelled" value={counts.cancelled} tone="text-rose-300" />
      </div>

      <div className="flex items-center gap-2">
        <Gauge className="h-4 w-4 text-cyan-300" />
        <div className="flex flex-wrap gap-1.5">
          <span className="chip">
            flights <span className="font-mono text-white/90">{snap?.results.flights ?? 0}</span>
          </span>
          <span className="chip">
            tickets <span className="font-mono text-white/90">{snap?.results.tickets ?? 0}</span>
          </span>
          <span className="chip">
            bookings <span className="font-mono text-white/90">{snap?.active.length ?? 0}</span>
          </span>
          {snap && snap.cancelled.length > 0 && (
            <span className="chip border-rose-500/25 bg-rose-500/[0.08]">
              cancelled <span className="font-mono text-rose-300">{snap.cancelled.length}</span>
            </span>
          )}
        </div>
      </div>
    </div>
  );
}

function Metric({ label, value, tone }: { label: string; value: number; tone: string }) {
  return (
    <motion.div
      key={`${label}-${value}`}
      initial={{ scale: 0.95, opacity: 0.6 }}
      animate={{ scale: 1, opacity: 1 }}
      className="rounded-xl border border-white/10 bg-white/[0.03] px-2 py-1.5 text-center"
    >
      <div className={`font-mono text-[16px] font-bold ${tone}`}>{value}</div>
      <div className="text-[9px] uppercase tracking-wider text-off">{label}</div>
    </motion.div>
  );
}

export function SignalPanel({
  onInterrupt,
  lastInterruptAt,
}: {
  onInterrupt: (r: string) => void;
  lastInterruptAt: number | null;
}) {
  const [, setTick] = useState(0);
  useEffect(() => {
    if (!lastInterruptAt) return;
    const iv = window.setInterval(() => setTick((t) => t + 1), 1000);
    return () => window.clearInterval(iv);
  }, [lastInterruptAt]);

  const styles: Array<{ label: string; reason: string }> = [
    { label: "Redirect", reason: "user_redirect" },
    { label: "Correct", reason: "user_correction" },
    { label: "Stop", reason: "user_override" },
  ];
  return (
    <div className="glass p-4">
      <div className="mb-3 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Ban className="h-4 w-4 text-rose-300" />
          <h3 className="text-[12px] font-semibold uppercase tracking-widest text-off">
            Barge-in
          </h3>
        </div>
        {lastInterruptAt && (
          <span className="text-[10px] font-medium uppercase tracking-wider text-rose-300">
            fired {Math.max(1, Math.round((Date.now() - lastInterruptAt) / 1000))}s ago
          </span>
        )}
      </div>
      <div className="grid grid-cols-3 gap-2">
        {styles.map((s) => (
          <button
            key={s.reason}
            onClick={() => onInterrupt(s.reason)}
            className="rounded-xl border border-rose-500/20 bg-rose-500/[0.06] px-2 py-2 text-[11px] font-semibold text-rose-200 transition hover:border-rose-400/40 hover:bg-rose-500/15 active:scale-95"
          >
            {s.label}
          </button>
        ))}
      </div>
      <p className="mt-2.5 text-[10px] leading-relaxed text-off/80">
        Interrupts ride the <span className="text-white/100">fast path</span>: pending calls that
        fired within 30&nbsp;ms of the turn get grace-cancelled, later ones are fenced out.
      </p>
    </div>
  );
}