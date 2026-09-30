import { useCallback, useEffect, useRef, useState } from "react";
import { appendUser, emptyFeed, reduceFeed, type FeedState } from "../lib/feed";
import type { ConnState, ServerMessage } from "../lib/types";

function openSocket(onMessage: (msg: ServerMessage) => void, onState: (c: ConnState) => void) {
  const proto = window.location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${window.location.host}/ws`);
  let closedByCleanup = false;

  ws.onopen = () => onState("open");
  ws.onclose = () => {
    if (!closedByCleanup) onState("closed");
  };
  ws.onerror = () => ws.close();
  ws.onmessage = (ev) => {
    try {
      onMessage(JSON.parse(ev.data as string) as ServerMessage);
    } catch {
      /* ignore malformed frames */
    }
  };
  return {
    socket: ws,
    close: () => {
      closedByCleanup = true;
      ws.close();
    },
  };
}

export function useAerupt() {
  const [feed, setFeed] = useState<FeedState>(emptyFeed);
  const [conn, setConn] = useState<ConnState>("connecting");
  const [manifestTools, setManifestTools] = useState<string[]>([]);
  const [connectedAt, setConnectedAt] = useState<number>(0);
  const wsRef = useRef<WebSocket | null>(null);
  const retryTimer = useRef<number | null>(null);
  const shouldRetry = useRef(true);

  useEffect(() => {
    let alive = true;

    const connect = () => {
      if (!alive || !shouldRetry.current) return;
      const handle = openSocket(
        (msg) => {
          if (!alive) return;
          if (msg.type === "ready") {
            setManifestTools((msg as unknown as { manifest?: string[] }).manifest ?? []);
            return;
          }
          setFeed((f) => reduceFeed(f, msg));
        },
        (c) => {
          if (!alive) return;
          if (c === "open") {
            setConn("open");
            setConnectedAt(Date.now());
          } else {
            setConn("closed");
            // keep trying to recover so controls never stay frozen
            if (retryTimer.current === null) {
              retryTimer.current = window.setTimeout(() => {
                retryTimer.current = null;
                connect();
              }, 2000);
            }
          }
        },
      );
      wsRef.current = handle.socket;
      return handle.close;
    };

    const cleanup = connect();
    return () => {
      alive = false;
      shouldRetry.current = false;
      if (retryTimer.current !== null) window.clearTimeout(retryTimer.current);
      cleanup?.();
    };
  }, []);

  const send = useCallback((obj: Record<string, unknown>) => {
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(obj));
    }
  }, []);

  const commit = useCallback(
    (text: string) => {
      send({ type: "transcript", text, end_of_turn: true });
    },
    [send],
  );

  const pushUser = useCallback((text: string) => {
    setFeed((f) => appendUser(f, text));
  }, []);

  const partial = useCallback(
    (text: string) => {
      send({ type: "transcript", text, end_of_turn: false, partial: true });
    },
    [send],
  );

  const interrupt = useCallback(
    (reason: string) => {
      send({ type: "interrupt", reason });
    },
    [send],
  );

  const reset = useCallback(() => {
    send({ type: "reset" });
    setFeed(emptyFeed());
  }, [send]);

  return { feed, conn, manifestTools, connectedAt, commit, pushUser, partial, interrupt, reset };
}