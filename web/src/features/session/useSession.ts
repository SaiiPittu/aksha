import { useEffect, useMemo, useReducer, useRef } from "react"
import { useNavigate } from "react-router"
import { useQueryClient } from "@tanstack/react-query"
import { useSessionsQuery, type SessionRow } from "@/api"
import type { SessionHandle, SessionSummary, Status } from "./types"
import { SessionClient } from "./client"
import { sessionCommands } from "./commands"
import { foldMessages, initialStore, type SessionStore } from "./reducer"
import type { LocalAction, StreamMessage } from "./wire"

// Optimistic first messages of sessions this tab just created, so the new session's store
// shows the message before its stream confirms it.
const seeds = new Map<string, LocalAction[]>()

type Action = { kind: "reset"; sessionId: string | null } | { kind: "batch"; sessionId: string | null; msgs: StreamMessage[] }

function init(sessionId: string | null): SessionStore {
  const s = initialStore(sessionId)
  const pending = sessionId ? seeds.get(sessionId) : undefined
  if (!pending) return s
  seeds.delete(sessionId!)
  return foldMessages(s, pending.map((action) => ({ kind: "local", action })))
}

function reduce(store: SessionStore, a: Action): SessionStore {
  if (a.kind === "reset") return init(a.sessionId)
  if (a.sessionId !== store.state.sessionId) return store
  return foldMessages(store, a.msgs)
}

/**
 * THE SEAM the chat screen renders: one live connection per open session, folded by the
 * pure reducer into `SessionState`, plus commands over the HTTP API.
 */
export function useSession(sessionId: string | null): SessionHandle {
  const [store, dispatch] = useReducer(reduce, sessionId, init)
  const navigate = useNavigate()
  const qc = useQueryClient()
  const ref = useRef(store)
  useEffect(() => { ref.current = store })

  // A different session: start over (the route keeps this hook mounted across sessions).
  if (store.state.sessionId !== sessionId) dispatch({ kind: "reset", sessionId })

  useEffect(() => {
    if (!sessionId) return
    const client = new SessionClient({
      sessionId,
      onMessages: (msgs) => dispatch({ kind: "batch", sessionId, msgs }),
      onResync: () => qc.invalidateQueries(),
    })
    client.start()
    return () => client.close()
  }, [sessionId, qc])

  // Lists show the new state of this session as its runs move.
  const status = store.state.runs.at(-1)?.status
  useEffect(() => { if (status) void qc.invalidateQueries({ queryKey: ["sessions"] }) }, [status, qc])

  const commands = useMemo(() => sessionCommands({
    getState: () => ref.current.state,
    dispatch: (action) => dispatch({ kind: "batch", sessionId: ref.current.state.sessionId, msgs: [{ kind: "local", action }] }),
    onNewSession: (id, seed) => {
      seeds.set(id, [seed])
      void qc.invalidateQueries({ queryKey: ["sessions"] })
      navigate(`/s/${encodeURIComponent(id)}`)
    },
  }), [navigate, qc])

  return { state: store.state, commands }
}

// ---- the sessions list (rail, sessions page, inbox) ---------------------------------

const statusOf: Record<string, { status: Status; label: string }> = {
  waiting: { status: "wait", label: "Waiting on you" },
  running: { status: "run", label: "Running" },
  done: { status: "ok", label: "Done" },
  complete: { status: "ok", label: "Done" },
  partial: { status: "wait", label: "Partial" },
  failed: { status: "bad", label: "Failed" },
  blocked: { status: "bad", label: "Blocked" },
  stopped: { status: "idle", label: "Stopped" },
  idle: { status: "idle", label: "Idle" },
}

export function ageOf(iso?: string | null, now = Date.now()): string {
  if (!iso) return ""
  const t = Date.parse(iso)
  if (Number.isNaN(t)) return ""
  const s = Math.max(0, (now - t) / 1000)
  if (s < 60) return "now"
  if (s < 3600) return `${Math.floor(s / 60)}m`
  if (s < 86400) return `${Math.floor(s / 3600)}h`
  return `${Math.floor(s / 86400)}d`
}

export function summaryOf(r: SessionRow): SessionSummary {
  const st = statusOf[r.status] ?? { status: "idle" as Status, label: r.status }
  return {
    id: r.session, title: r.title || r.session, agent: r.agent ?? "Auto-route", status: st.status, statusLabel: st.label,
    age: ageOf(r.last_at ?? r.started_at), waiting: r.waiting ?? undefined, archived: r.archived, run: r.run ?? undefined,
    lastAt: r.last_at ?? r.started_at ?? undefined,
  }
}

/** The rail, the sessions page and the inbox: GET /sessions. */
export function useSessionList(archived = false): { sessions: SessionSummary[]; loading: boolean; error?: unknown } {
  const q = useSessionsQuery(archived)
  const sessions = useMemo(() => (q.data ?? []).map(summaryOf), [q.data])
  return { sessions, loading: q.isLoading, error: q.error ?? undefined }
}
