import { useCallback, useEffect, useRef, useState } from "react";
import { ChatFeed } from "./components/ChatFeed";
import { Composer } from "./components/Composer";
import { Header } from "./components/Header";
import { SignalPanel, SnapshotPanel } from "./components/Panels";
import { VoiceSphere, type SpherePhase } from "./components/VoiceSphere";
import { useAerupt } from "./hooks/useAerupt";
import { useSpeechOutput } from "./hooks/useSpeechOutput";
import type { ConnState, StampedMessage } from "./lib/types";

function derivePhase(items: StampedMessage[]): SpherePhase {
  for (let i = items.length - 1; i >= 0; i--) {
    const k = items[i].kind;
    if (k === "cancel") return "interrupt";
    if (k === "response") return "done";
    if (k === "user") return "listen";
    if (k === "tool_call") return "tool";
    if (k === "narration" || k === "filler") return "speak";
  }
  return "idle";
}

export default function App() {
  const { feed, conn, manifestTools, commit, pushUser, partial, interrupt, reset } = useAerupt();
  const [speaking, setSpeaking] = useState(false);
  const [micActive, setMicActive] = useState(false);
  const [lastInterruptAt, setLastInterruptAt] = useState<number | null>(null);
  const speakTimer = useRef<number | null>(null);
  const voice = useSpeechOutput(feed.items, micActive);

  useEffect(() => {
    const last = feed.items[feed.items.length - 1];
    if (!last) {
      setSpeaking(false);
      return;
    }
    const clearTimer = () => {
      if (speakTimer.current) {
        window.clearTimeout(speakTimer.current);
        speakTimer.current = null;
      }
    };
    if (last.kind === "user" || last.kind === "cancel") {
      setSpeaking(false);
      clearTimer();
      return;
    }
    let ms = 0;
    if (last.kind === "narration" || last.kind === "filler" || last.kind === "response") {
      const len = String(last.data.text ?? "").length;
      ms = Math.max(1200, len * 45 + 350);
    } else if (last.kind === "tool_result") {
      ms = 900;
    }
    setSpeaking(true);
    if (ms > 0) {
      clearTimer();
      speakTimer.current = window.setTimeout(() => setSpeaking(false), ms);
    }
  }, [feed.items]);

  useEffect(() => {
    if (conn === "closed") setSpeaking(false);
  }, [conn]);

  const doCommit = useCallback(
    (text: string) => {
      pushUser(text);
      commit(text);
    },
    [pushUser, commit],
  );

  const onInterrupt = useCallback(
    (reason: string) => {
      setLastInterruptAt(Date.now());
      interrupt(reason);
    },
    [interrupt],
  );

  const onReset = useCallback(() => {
    reset();
    setSpeaking(false);
    setLastInterruptAt(null);
  }, [reset]);

  const connTyped: ConnState = conn;
  const phase: SpherePhase = micActive ? "listen" : derivePhase(feed.items);

  return (
    <div className="relative flex h-full flex-col">
      <div className="aurora">
        <span className="left-[8%] top-[2%] h-[380px] w-[380px] bg-violet-600/15" />
        <span
          className="right-[6%] top-[6%] h-[320px] w-[320px] bg-indigo-600/12"
          style={{ animationDelay: "-6s" }}
        />
        <span
          className="bottom-[-30%] left-[35%] h-[420px] w-[420px] bg-cyan-500/8"
          style={{ animationDelay: "-11s" }}
        />
      </div>

      <Header
        conn={connTyped}
        speaking={speaking}
        tools={manifestTools}
        muted={voice.muted}
        speechSupported={voice.supported}
        onToggleMute={() => voice.setMuted((m) => !m)}
        onReset={onReset}
      />

      <div className="relative z-10 min-h-0">
        <VoiceSphere phase={phase} items={feed.items} />
      </div>

      <div className="relative z-10 grid min-h-0 flex-1 grid-cols-1 lg:grid-cols-[1fr_304px]">
        <main className="min-h-0">
          <ChatFeed feed={feed} onReply={doCommit} />
        </main>
        <aside className="hidden min-h-0 flex-col gap-3 overflow-y-auto border-l border-white/10 p-4 lg:flex">
          <SignalPanel onInterrupt={onInterrupt} lastInterruptAt={lastInterruptAt} />
          <SnapshotPanel feed={feed} />
          <div className="glass p-4">
            <h3 className="mb-2 text-[12px] font-semibold uppercase tracking-widest text-off">
              Pipeline
            </h3>
            <p className="text-[11px] leading-relaxed text-off/80">
              Mock tool latency ~<span className="font-mono text-white/90">650&nbsp;ms</span>.
              AERUPT commits partial thoughts for speculative search while you type or talk, and
              barge-ins grace-cancel calls fired within 30&nbsp;ms of the turn. Barge in while it is speaking the response and the voice cuts instantly.
            </p>
          </div>
        </aside>
      </div>

      <Composer
        conn={connTyped}
        onCommit={doCommit}
        onPartial={partial}
        onInterrupt={onInterrupt}
        onMicState={setMicActive}
      />
    </div>
  );
}