import { useCallback, useEffect, useRef, useState } from "react";

export type SpeechState = "idle" | "busy" | "listening" | "error" | "unsupported";

interface SpeechRecognitionLike {
  lang: string;
  continuous: boolean;
  interimResults: boolean;
  maxAlternatives: number;
  onresult: ((e: SpeechResultEvent) => void) | null;
  onend: (() => void) | null;
  onerror: ((e: { error?: string }) => void) | null;
  start(): void;
  stop(): void;
  abort(): void;
}

interface SpeechResultEvent {
  resultIndex: number;
  results: ArrayLike<{ isFinal: boolean; 0: { transcript: string } }>;
}

function getSRCtor(): (new () => SpeechRecognitionLike) | null {
  if (typeof window === "undefined") return null;
  const w = window as unknown as Record<string, unknown>;
  const C = (w.SpeechRecognition ?? w.webkitSpeechRecognition) as
    | (new () => SpeechRecognitionLike)
    | undefined;
  return C ?? null;
}

/** Real microphone input via the Web Speech API.
 *  Interim transcript streams to onPartial (so AERUPT speculates while you talk),
 *  the final utterance is sent through onCommit. */
export function useSpeech(onPartial: (t: string) => void, onCommit: (t: string) => void) {
  const SR = getSRCtor();
  const supported = SR !== null;
  const [state, setState] = useState<SpeechState>(supported ? "idle" : "unsupported");
  const [interim, setInterim] = useState("");
  const srRef = useRef<SpeechRecognitionLike | null>(null);
  const activeRef = useRef(false);
  const onPartialRef = useRef(onPartial);
  const onCommitRef = useRef(onCommit);
  onPartialRef.current = onPartial;
  onCommitRef.current = onCommit;

  const stop = useCallback(() => {
    activeRef.current = false;
    try {
      srRef.current?.stop();
    } catch {
      /* already stopped */
    }
  }, []);

  const toggle = useCallback(() => {
    if (!SR) return;
    if (activeRef.current) {
      stop();
      return;
    }
    activeRef.current = true;
    setInterim("");
    setState("busy");
    const rec = new SR();
    srRef.current = rec;
    rec.lang = "en-US";
    rec.continuous = true;
    rec.interimResults = true;
    rec.maxAlternatives = 1;
    let finalText = "";
    rec.onresult = (e) => {
      let interimText = "";
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const res = e.results[i];
        if (res.isFinal) finalText += (res[0]?.transcript ?? "") + " ";
        else interimText += res[0]?.transcript ?? "";
      }
      const trimmed = interimText.trim();
      setInterim(trimmed);
      setState("listening");
      if (trimmed.length >= 3) onPartialRef.current(trimmed);
    };
    rec.onend = () => {
      activeRef.current = false;
      const ft = finalText.trim();
      if (ft) onCommitRef.current(ft);
      setInterim("");
      setState("idle");
      srRef.current = null;
    };
    rec.onerror = (e) => {
      const err = e.error ?? "unknown";
      if (err === "not-allowed" || err === "service-not-allowed") {
        activeRef.current = false;
        setState("error");
      } else if (err === "no-speech" || err === "aborted") {
        setState("listening");
      }
    };
    try {
      rec.start();
    } catch {
      activeRef.current = false;
      setState("error");
    }
  }, [SR, stop]);

  useEffect(() => () => stop(), [stop]);

  return { state, interim, supported, toggle, stop };
}