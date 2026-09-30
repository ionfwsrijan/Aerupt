import { useCallback, useEffect, useRef, useState } from "react";
import type { StampedMessage } from "../lib/types";

function pickVoice(): SpeechSynthesisVoice | null {
  const vs = window.speechSynthesis.getVoices();
  return (
    vs.find((v) => /en[-_](US|GB)/i.test(v.lang) && /google|microsoft|samantha|daniel|zira|natural/i.test(v.name)) ??
    vs.find((v) => /^en/i.test(v.lang)) ??
    vs[0] ??
    null
  );
}

/** Spoken output for AERUPT: local speechSynthesis, grounded on the same
 *  narration/response events the chat types. Interrupts and new turns cut
 *  speech instantly — the "talking while you barge in" moment. */
export function useSpeechOutput(items: StampedMessage[], micActive: boolean) {
  const supported = typeof window !== "undefined" && "speechSynthesis" in window;
  const [muted, setMuted] = useState(false);
  const mutedRef = useRef(muted);
  const micRef = useRef(micActive);
  const lastId = useRef(0);
  mutedRef.current = muted;
  micRef.current = micActive;

  // warm the voice list (getVoices() populates asynchronously)
  useEffect(() => {
    if (!supported) return;
    if (typeof window.speechSynthesis.onvoiceschanged === "function") {
      window.speechSynthesis.onvoiceschanged = () => pickVoice();
    }
    void pickVoice();
  }, [supported]);

  const cancel = useCallback(() => {
    if (supported) window.speechSynthesis.cancel();
  }, [supported]);

  useEffect(() => {
    if (!supported) return;
    const last = items[items.length - 1];
    if (!last) return;
    // user took the floor or interrupted → cut speech mid-sentence
    if (last.kind === "user" || last.kind === "cancel") {
      cancel();
      return;
    }
    if (last.id <= lastId.current) return;
    lastId.current = last.id;
    if (
      last.kind !== "narration" &&
      last.kind !== "filler" &&
      last.kind !== "response" &&
      last.kind !== "clarify"
    ) {
      return;
    }
    if (mutedRef.current || micRef.current) return;
    const text = String(last.data.text ?? "").trim();
    if (!text) return;
    const u = new SpeechSynthesisUtterance(text);
    u.rate = 1.06;
    u.pitch = 1;
    u.volume = 1;
    const v = pickVoice();
    if (v) u.voice = v;
    window.speechSynthesis.cancel();
    window.speechSynthesis.speak(u);
  }, [items, supported, cancel]);

  // opening the mic or muting always stops any in-flight speech
  useEffect(() => {
    if ((micActive || muted) && supported) cancel();
  }, [micActive, muted, supported, cancel]);

  return { muted, setMuted, supported, cancel };
}