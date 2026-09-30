import type {
  ActionKind,
  ServerMessage,
  Snapshot,
  StampedMessage,
  ToolItem,
  ToolStatus,
} from "./types";

let uid = 0;
export const nextId = () => ++uid;

export interface FeedState {
  items: StampedMessage[];
  toolById: Map<string, ToolItem>;
}

export const emptyFeed = (): FeedState => ({ items: [], toolById: new Map() });

function stamp(
  kind: ActionKind,
  ts: number,
  data: Record<string, unknown>,
): StampedMessage {
  return { id: nextId(), kind, ts, data };
}

function toolFrom(m: StampedMessage): ToolItem | null {
  if (m.kind !== "tool_call") return null;
  const callId = String(m.data.call_id ?? "");
  if (!callId) return null;
  return {
    ...m,
    kind: "tool_call",
    callId,
    tool: String(m.data.tool ?? "tool"),
    args: (m.data.args as Record<string, unknown>) ?? {},
    speculative: Boolean(m.data.speculative),
    status: "running",
  };
}

function summarizeResult(tool: string, ok: boolean, result: Record<string, unknown>): string {
  if (!ok) return "failed";
  if (tool === "flight_search") {
    const flights = (result.flights as Array<{ flight_id: string; price: number }>) ?? [];
    if (!flights.length) return "no flights";
    return flights
      .slice(0, 3)
      .map((f) => `${f.flight_id} $${f.price}`)
      .join(" · ");
  }
  if (tool === "book_flight") {
    const b = (result.booking as { reference?: string }) ?? {};
    return `booked · ${b.reference ?? "—"}`;
  }
  if (tool === "cancel_booking") return "cancelled";
  if (tool === "create_ticket") return "ticket created";
  if (tool === "lookup_manual") return "manual page scanned";
  if (tool === "kb_lookup") {
    const sources = (result.sources as Array<{ source?: string }>) ?? [];
    if (!sources.length) return "no KB hit";
    return "knowledge base: " + sources
      .slice(0, 2)
      .map((s) => s.source ?? "?")
      .join(" · ");
  }
  return "ok";
}

export function snapshotFrom(m: StampedMessage): Snapshot | null {
  const data = (m.data?.snapshot as Record<string, unknown>) ?? null;
  if (!data) return null;
  return {
    intent: String(data.intent ?? ""),
    slots: (data.slots as Record<string, unknown>) ?? {},
    active: ((data.active as Array<Record<string, unknown>>) ?? []).map((a) => ({
      tool: String(a.tool ?? ""),
      flight_id: a.flight_id ? String(a.flight_id) : undefined,
      reference: a.reference ? String(a.reference) : undefined,
    })),
    cancelled: ((data.cancelled as Array<Record<string, unknown>>) ?? []).map((c) => ({
      flight_id: c.flight_id ? String(c.flight_id) : undefined,
    })),
    results: (data.results as Record<string, number>) ?? {},
  };
}

export function appendUser(state: FeedState, text: string): FeedState {
  const msg: StampedMessage = {
    id: nextId(),
    kind: "user",
    ts: Date.now() / 1000,
    data: { text },
    text,
  };
  return { ...state, items: [...state.items, msg] };
}

export function latestSnapshot(state: FeedState): Snapshot | null {
  for (let i = state.items.length - 1; i >= 0; i--) {
    const s = snapshotFrom(state.items[i]);
    if (s) return s;
  }
  return null;
}

export function reduceFeed(state: FeedState, msg: ServerMessage): FeedState {
  const items = [...state.items];
  const toolById = new Map(state.toolById);
  const kind = msg.kind as ActionKind;

  const patch = (cid: string, fn: (t: ToolItem) => Partial<ToolItem>) => {
    const cur = toolById.get(cid);
    if (!cur) return;
    const next: ToolItem = { ...cur, ...fn(cur) };
    toolById.set(cid, next);
    const i = items.findIndex((x) => x.id === cur.id);
    if (i >= 0) items[i] = next;
  };

  switch (kind) {
    case "tool_call": {
      const t = toolFrom(stamp(kind, msg.ts, msg.data));
      if (t) {
        toolById.set(t.callId, t);
        items.push(t);
      }
      break;
    }
    case "tool_result": {
      const d = msg.data as Record<string, unknown>;
      const cid = String(d.call_id ?? "");
      const ok = Boolean(d.ok);
      patch(cid, (cur) => ({
        status: (ok ? "done" : "error") as ToolStatus,
        ok,
        error: d.error ? String(d.error) : undefined,
        summary: summarizeResult(String(d.tool ?? cur.tool), ok, (d.result as Record<string, unknown>) ?? {}),
      }));
      break;
    }
    case "cancel": {
      const cids = (msg.data.call_ids as string[]) ?? [];
      for (const cid of cids) patch(cid, () => ({ status: "cancelled" as ToolStatus }));
      items.push(stamp(kind, msg.ts, msg.data));
      break;
    }
    case "clarify": {
      items.push(stamp(kind, msg.ts, msg.data));
      break;
    }
    default: {
      items.push(stamp(kind, msg.ts, msg.data));
    }
  }

  return { items, toolById };
}

export const toolCount = (state: FeedState) => {
  let calls = 0;
  let resolved = 0;
  let cancelled = 0;
  state.items.forEach((i) => {
    if (i.kind !== "tool_call") return;
    const it = i as ToolItem;
    calls += 1;
    const st = it.status;
    if (st === "done" || st === "error") resolved += 1;
    if (st === "cancelled") cancelled += 1;
  });
  return { calls, resolved, cancelled };
};

export function flattenSlots(slots: Record<string, unknown>): Array<{ key: string; value: string }> {
  return Object.entries(slots)
    .filter(([, v]) => v !== undefined && v !== null && v !== "")
    .map(([key, value]) => ({ key, value: String(value) }));
}