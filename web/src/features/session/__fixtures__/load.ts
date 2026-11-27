// Loads a recorded stream (`*.jsonl`: one parsed SSE block per line) as stream messages.
// The recordings come from the bundled fictional sample world; tool output is trimmed.
import type { StreamMessage } from "../wire"

export interface Block { sse_id?: string; sse_event?: string; data?: unknown; comment?: string }

const files = import.meta.glob("./*.jsonl", { query: "?raw", import: "default", eager: true }) as Record<string, string>

export function blocks(name: string): Block[] {
  const raw = files[`./${name}`]
  if (raw == null) throw new Error(`no fixture ${name}`)
  return raw.split("\n").filter(Boolean).map((l: string) => JSON.parse(l) as Block)
}

/** The stream messages of a recording, in order (comments such as heartbeats dropped). */
export function stream(name: string): StreamMessage[] {
  return blocks(name).filter((b) => b.data && typeof b.data === "object").map((b) => b.data as StreamMessage)
}
