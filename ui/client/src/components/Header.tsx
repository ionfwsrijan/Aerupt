import { motion } from "framer-motion";
import { RotateCcw, Volume2, VolumeX, Zap } from "lucide-react";
import type { ConnState } from "../lib/types";

function statusMeta(conn: ConnState, speaking: boolean) {
  if (conn !== "open") return { label: "offline", tone: "bg-rose-500/80", pulse: false };
  if (speaking) return { label: "speaking", tone: "bg-cyan-400", pulse: true };
  return { label: "listening", tone: "bg-emerald-400", pulse: true };
}

export function Header({
  conn,
  speaking,
  tools,
  muted,
  speechSupported,
  onToggleMute,
  onReset,
}: {
  conn: ConnState;
  speaking: boolean;
  tools: string[];
  muted: boolean;
  speechSupported: boolean;
  onToggleMute: () => void;
  onReset: () => void;
}) {
  const st = statusMeta(conn, speaking);
  return (
    <header className="relative z-20 flex items-center gap-4 border-b border-white/[0.06] px-6 py-3">
      <div className="relative h-8 w-8">
        <motion.div
          className="absolute inset-0 rounded-xl bg-gradient-to-br from-indigo-500 via-violet-500 to-cyan-400"
          animate={speaking ? { scale: [1, 1.12, 1] } : {}}
          transition={{ duration: 1.4, repeat: Infinity, ease: "easeInOut" }}
        />
        <div className="absolute inset-[3px] rounded-[10px] bg-ink" />
        <Zap className="absolute inset-0 m-auto h-3.5 w-3.5 text-cyan-300" strokeWidth={2.4} />
      </div>
      <div className="min-w-0">
        <div className="flex items-baseline gap-2">
          <h1 className="text-[15px] font-bold tracking-tight text-white">AERUPT</h1>
          <span className="text-[11px] font-medium uppercase tracking-[0.18em] text-off">
            interruptible real-time agent
          </span>
        </div>
      </div>
      <div className="ml-auto flex items-center gap-3">
        {tools.length > 0 && (
          <div className="hidden items-center gap-1.5 md:flex">
            {tools.map((t) => (
              <span
                key={t}
                className="rounded-full border border-white/10 bg-white/[0.04] px-2 py-0.5 font-mono text-[10px] text-off"
              >
                {t}
              </span>
            ))}
          </div>
        )}
        <div className="flex items-center gap-2 rounded-full border border-white/[0.08] bg-white/[0.03] px-3 py-1.5">
          <span className="relative flex h-2 w-2">
            <span
              className={`absolute inline-flex h-full w-full rounded-full ${st.tone} ${
                st.pulse ? "animate-ping opacity-60" : ""
              }`}
            />
            <span className={`relative inline-flex h-2 w-2 rounded-full ${st.tone}`} />
          </span>
          <span className="text-[11px] font-medium uppercase tracking-widest text-white/100">
            {st.label}
          </span>
        </div>
        {speechSupported && (
          <button
            onClick={onToggleMute}
            title={muted ? "Unmute AERUPT's voice" : "Mute AERUPT's voice"}
            className="btn-ghost"
          >
            {muted ? <VolumeX className="h-3.5 w-3.5" /> : <Volume2 className="h-3.5 w-3.5" />}
            {muted ? "Muted" : "Voice"}
          </button>
        )}
        <button onClick={onReset} title="Reset session" className="btn-ghost">
          <RotateCcw className="h-3.5 w-3.5" />
          Reset
        </button>
      </div>
    </header>
  );
}