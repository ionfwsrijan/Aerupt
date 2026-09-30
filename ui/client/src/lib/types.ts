export type ActionKind =
  | "narration"
  | "filler"
  | "tool_call"
  | "cancel"
  | "tool_result"
  | "clarify"
  | "response";

export interface ServerMessage {
  type: string;
  kind: string;
  ts: number;
  data: Record<string, unknown>;
}

export interface UserMessage {
  id: number;
  kind: "user";
  ts: number;
  text: string;
  partial?: boolean;
}

export interface StampedMessage {
  id: number;
  kind: ActionKind | "user" | "system";
  ts: number;
  data: Record<string, unknown>;
  text?: string;
}

export type ToolStatus = "running" | "done" | "cancelled" | "error" | "superseded";

export interface ToolItem extends StampedMessage {
  kind: "tool_call";
  callId: string;
  tool: string;
  args: Record<string, unknown>;
  speculative: boolean;
  status: ToolStatus;
  ok?: boolean;
  error?: string;
  summary?: string;
}

export interface Snapshot {
  intent: string;
  slots: Record<string, unknown>;
  active: Array<{ tool: string; flight_id?: string; reference?: string }>;
  cancelled: Array<{ flight_id?: string }>;
  results: Record<string, number>;
  last_response?: string;
}

export interface AeruptSnapshot {
  intent?: string;
  slots?: Record<string, unknown>;
  active?: Array<Record<string, unknown>>;
  cancelled?: Array<Record<string, unknown>>;
  results?: Record<string, number>;
  [k: string]: unknown;
}

export type ConnState = "connecting" | "open" | "closed";

export interface SlotChip {
  key: string;
  label: string;
  value: string;
}