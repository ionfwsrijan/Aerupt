import { useEffect, useRef } from "react";
import { AnimatePresence, animate, motion, useReducedMotion } from "framer-motion";
import type { StampedMessage } from "../lib/types";

export type SpherePhase = "idle" | "listen" | "speak" | "tool" | "done" | "interrupt";

export const PHASE_META: Record<
  SpherePhase,
  { verb: string; tint: string; dusk: string; burst: number }
> = {
  idle: { verb: "at your service", tint: "150, 170, 255", dusk: "99, 102, 241", burst: 0.5 },
  listen: { verb: "listening…", tint: "255, 204, 120", dusk: "234, 140, 60", burst: 0.55 },
  speak: { verb: "thinking aloud", tint: "255, 214, 150", dusk: "244, 160, 66", burst: 0.95 },
  tool: { verb: "acting on it…", tint: "110, 235, 255", dusk: "8, 145, 178", burst: 0.85 },
  done: { verb: "done — your move", tint: "160, 180, 255", dusk: "99, 102, 241", burst: 1.25 },
  interrupt: { verb: "redirecting…", tint: "255, 130, 130", dusk: "244, 63, 94", burst: 1.35 },
};

const LISTEN_MORPH = [
  "50% 50% 50% 50% / 50% 50% 50% 50%",
  "41% 59% 58% 42% / 64% 44% 56% 36%",
  "57% 43% 44% 56% / 40% 58% 42% 60%",
  "46% 54% 52% 48% / 60% 46% 54% 40%",
  "50% 50% 50% 50% / 50% 50% 50% 50%",
];

function WaveRing({
  margin,
  dur,
  reverse,
  dash,
  spin,
}: {
  margin: number;
  dur: number;
  reverse?: boolean;
  dash: number;
  spin: boolean;
}) {
  return (
    <motion.div
      className="absolute inset-0"
      animate={spin ? { rotate: reverse ? [360, 0] : [0, 360] } : undefined}
      transition={{ duration: dur, repeat: Infinity, ease: "linear" }}
    >
      <svg
        className="h-full w-full"
        viewBox="0 0 100 100"
        fill="none"
        style={{ transform: `scale(${margin})` }}
      >
        <circle
          cx="50"
          cy="50"
          r="48"
          stroke="var(--sphere-tint)"
          strokeOpacity="0.5"
          strokeWidth="1"
          strokeDasharray={dash}
          strokeLinecap="round"
          style={{ filter: "drop-shadow(0 0 6px var(--sphere-tint))" }}
        />
      </svg>
    </motion.div>
  );
}

/** Equalizer ticks orbiting the orb — the "waveform sweeping" while AERUPT speaks. */
function WaveformRing() {
  const bars = 28;
  const R = 112;
  return (
    <motion.div
      initial={{ opacity: 0, scale: 0.82 }}
      animate={{ opacity: 1, scale: 1 }}
      exit={{ opacity: 0, scale: 0.82 }}
      transition={{ duration: 0.5 }}
      className="absolute left-1/2 top-1/2"
      style={{ width: 0, height: 0 }}
    >
      <motion.div
        className="relative"
        animate={{ rotate: 360 }}
        transition={{ duration: 22, repeat: Infinity, ease: "linear" }}
      >
        {Array.from({ length: bars }).map((_, i) => {
          const a = (360 / bars) * i;
          return (
            <motion.div
              key={i}
              className="absolute left-0 top-0"
              style={{
                transform: `rotate(${a}deg) translateY(${-R}px)`,
                transformOrigin: "center",
              }}
              animate={{ height: [6, 18, 8, 15, 5] }}
              transition={{
                duration: 1.05,
                repeat: Infinity,
                delay: i * 0.045,
                ease: "easeInOut",
              }}
            >
              <div
                className="w-[2.5px] rounded-full"
                style={{
                  height: "100%",
                  background: "linear-gradient(to top, rgba(8,10,26,0), var(--sphere-tint))",
                  boxShadow: "0 0 6px var(--sphere-tint)",
                }}
              />
            </motion.div>
          );
        })}
      </motion.div>
    </motion.div>
  );
}

export function VoiceSphere({ phase, items }: { phase: SpherePhase; items: StampedMessage[] }) {
  const reduced = useReducedMotion();
  const ringRefs = [
    useRef<HTMLDivElement>(null),
    useRef<HTMLDivElement>(null),
    useRef<HTMLDivElement>(null),
  ];
  const lastId = useRef<number | null>(null);
  const meta = PHASE_META[phase];
  const listening = phase === "listen" && !reduced;
  const talking = (phase === "speak" || phase === "done") && !reduced;

  useEffect(() => {
    const html = document.documentElement;
    html.style.setProperty("--sphere-tint", `rgb(${meta.tint})`);
    html.style.setProperty("--sphere-dusk", `rgb(${meta.dusk})`);
    html.style.setProperty("--sphere-glow", `rgba(${meta.dusk}, 0.32)`);
    html.style.setProperty("--sphere-glow-soft", `rgba(${meta.dusk}, 0.14)`);
  }, [phase, meta]);

  useEffect(() => {
    if (reduced || !items.length) return;
    const last = items[items.length - 1];
    if (last.id === lastId.current) return;
    lastId.current = last.id;
    const strength = last.kind === "response" ? meta.burst : PHASE_META[phase].burst;
    ringRefs.forEach((r, i) => {
      const el = r.current;
      if (!el) return;
      animate(
        el,
        {
          scale: [0.82, 1.3 + strength * 0.5 + i * 0.16, 1.0],
          opacity: [0, 0.3 + strength * 0.16, 0],
        },
        { duration: 0.7 + i * 0.22, ease: "easeOut", delay: i * 0.1 },
      );
    });
  }, [items, phase, reduced, meta.burst]);

  return (
    <div
      aria-hidden
      className="pointer-events-none relative z-0 mx-auto flex h-[272px] w-full max-w-4xl items-center justify-center"
    >
      {/* ambient back-glow */}
      <div
        className="absolute h-72 w-72 rounded-full blur-3xl transition-[background] duration-700"
        style={{ background: "radial-gradient(circle, var(--sphere-glow-soft) 0%, transparent 72%)" }}
      />

      {/* static orbit guides */}
      {[40, 84, 132].map((s) => (
        <div
          key={s}
          className="absolute rounded-full border border-white/[0.05]"
          style={{ width: 180 + s, height: 180 + s }}
        />
      ))}

      {/* roaming voice-wave arcs */}
      <WaveRing margin={1} dur={26} dash={150} spin />
      <WaveRing margin={0.92} dur={34} reverse dash={92} spin />
      <WaveRing margin={0.84} dur={30} dash={60} spin />

      {/* waveform sweeping while speaking */}
      <AnimatePresence>{talking && <WaveformRing key="wave" />}</AnimatePresence>

      {/* sound-reactive pulse rings */}
      {ringRefs.map((r, i) => (
        <div
          key={i}
          ref={r}
          className="absolute rounded-full border border-[var(--sphere-tint)] opacity-0"
          style={{ width: 250, height: 250, boxShadow: "0 0 18px var(--sphere-glow)" }}
        />
      ))}
      <div
        className="pulse-soft absolute rounded-full border border-[var(--sphere-tint)]"
        style={{ width: 250, height: 250, animationDelay: "1.1s" }}
      />

      {/* the sphere itself — morphs while listening */}
      <motion.div
        animate={reduced ? {} : { y: [0, -9, 0] }}
        transition={{ duration: 5.5, repeat: Infinity, ease: "easeInOut" }}
        className="relative h-40 w-40"
      >
        <motion.div
          className="absolute inset-0 overflow-hidden rounded-full"
          animate={
            listening
              ? { borderRadius: LISTEN_MORPH, scale: [1, 1.07, 1] }
              : { borderRadius: "50% 50% 50% 50% / 50% 50% 50% 50%", scale: 1 }
          }
          transition={
            listening
              ? { borderRadius: { duration: 2.4, repeat: Infinity, ease: "easeInOut" }, scale: { duration: 1.6, repeat: Infinity, ease: "easeInOut" } }
              : { duration: 0.6 }
          }
        >
          <div
            className="absolute inset-0 transition-[box-shadow] duration-700"
            style={{
              background:
                "radial-gradient(circle at 30% 26%, rgba(255,255,255,0.98) 0%, var(--sphere-tint) 30%, var(--sphere-dusk) 62%, rgba(8,10,26,0.92) 96%)",
              boxShadow: "0 0 70px 20px var(--sphere-glow)",
            }}
          />
          <div className="spinner-shimmer absolute inset-0 opacity-60" />
          <div
            className="absolute inset-0"
            style={{
              background:
                "radial-gradient(120% 55% at 72% 108%, rgba(255,255,255,0.4), transparent 46%)",
            }}
          />
          <div
            className="absolute inset-0"
            style={{
              boxShadow:
                "inset -16px -18px 30px rgba(5,7,22,0.85), inset 12px 14px 26px rgba(255,255,255,0.2)",
            }}
          />
        </motion.div>
        <motion.div
          className="absolute -inset-3 rounded-full border border-white/10"
          animate={reduced ? {} : { scale: [1, 1.05, 1] }}
          transition={{ duration: listening ? 1.8 : 2.8, repeat: Infinity, ease: "easeInOut" }}
        />
      </motion.div>

      {/* status verb */}
      <motion.div
        key={phase}
        initial={{ opacity: 0, y: 6 }}
        animate={{ opacity: 1, y: 0 }}
        className="absolute bottom-6 left-1/2 flex -translate-x-1/2 items-center gap-2 rounded-full border border-white/10 bg-white/[0.04] px-3 py-1.5 backdrop-blur-xl"
      >
        <span className="relative flex h-1.5 w-1.5">
          <span
            className="absolute inline-flex h-full w-full animate-ping rounded-full opacity-60"
            style={{ background: "var(--sphere-tint)" }}
          />
          <span
            className="relative inline-flex h-1.5 w-1.5 rounded-full"
            style={{ background: "var(--sphere-tint)" }}
          />
        </span>
        <span className="text-[10.5px] font-semibold uppercase tracking-[0.2em] text-white/90">
          {meta.verb}
        </span>
      </motion.div>
    </div>
  );
}