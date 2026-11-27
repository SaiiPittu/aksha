// The session stream as it arrives on the wire (GET /sessions/{s}/events), and the
// messages the reducer folds. Shapes follow the observed contract; every field the reducer
// reads is optional, so a missing or extra field never breaks the fold.

import type { StreamState, ViewRef } from "./types"

/** One runtime event: `data` of an SSE block whose `kind` is "event". */
export interface WireEvent {
  kind: "event"
  seq: number
  type: string
  run?: string | null
  instance?: string | null
  step?: string | null
  session?: string | null
  at?: string
  payload?: Record<string, unknown>
}

/** Things only this browser tab knows: optimistic messages, in-flight commands, uploads. */
export type LocalAction =
  | { type: "optimistic-message"; commandId: string; text: string; at: string; author?: string; lane?: "steer" | "follow_up" | "amend"; files?: ViewRef[]; agent?: string }
  | { type: "command-refused"; commandId: string }
  | { type: "submitting"; interruptId: string; on: boolean }
  | { type: "uploaded"; files: ViewRef[] }
  | { type: "failed"; code: string; message: string }

export type StreamMessage =
  | WireEvent
  | { kind: "synchronized"; seq: number }
  | { kind: "resync"; seq: number }
  | { kind: "stream"; state: StreamState }
  | { kind: "local"; action: LocalAction }

/** Event types the reducer knows. Anything else is skipped (the cursor still advances). */
export const KNOWN_EVENT_TYPES = [
  "user.message", "route.decided", "run.started", "task.entered", "step.started", "step.completed",
  "artifact.saved", "citation.bound", "interrupt.raised", "interrupt.resolved", "interrupt.voided",
  "approval.requested", "approval.granted", "approval.rejected", "approval.voided",
  "input.queued", "input.consumed", "run.stop_requested", "run.finalizing", "run.ended",
  "document.stored", "source.changed", "outbox.enqueued", "outbox.claimed", "outbox.completed", "outbox.abandoned",
  "command.accepted", "command.refused", "budget.reached", "budget.granted", "review.accepted", "review.rejected",
  // bookkeeping: known, folded into nothing visible
  "stop.decided", "completion_check.evaluated", "read.recorded", "excerpts.minted", "excerpts.shown",
  "query.reexecuted",
] as const
