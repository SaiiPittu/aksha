import type { SessionState } from "./types"

/** The state before anything is known: a new chat, or a session whose stream has not opened. */
export function emptySessionState(sessionId: string | null): SessionState {
  return {
    sessionId,
    phase: sessionId ? "loading" : "idle",
    title: sessionId ? sessionId : "New chat",
    messages: [],
    runs: [],
    interrupts: {},
    artifacts: [],
    stream: sessionId ? "connecting" : "live",
    cursor: null,
  }
}
