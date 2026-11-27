// The session reducer: folds stream messages into the `SessionState` the chat screen renders.
//
// Pure and deterministic. The SAME function folds a replay and live events, so a reload, a
// reconnect and a live session all read the same thread. Events are deduplicated by seq
// (anything at or below the cursor is skipped), so folding a stream twice is a no-op.
// Unknown event types are skipped but still advance the cursor.

import type {
  ArtifactCitation, Interrupt, Message, Part, Run, SessionState, Status, ToolPart, ViewKind, ViewRef,
} from "./types"
import { emptySessionState } from "./state"
import { KNOWN_EVENT_TYPES, type LocalAction, type StreamMessage, type WireEvent } from "./wire"

const KNOWN: ReadonlySet<string> = new Set(KNOWN_EVENT_TYPES)

// ---- the store: the view model plus the indexes the fold needs ------------------

interface RunIdx {
  agent: string
  startedAt?: string
  lastAt?: string
  turns: number
  tokens: number
  /** The run's current agent message in the thread, and how many it has had. */
  segment?: string
  segments: number
  /** A notice to lead the run's first message with (classifier route, rebuild). */
  lead?: string
  ended?: boolean
}

interface Index {
  runs: Record<string, RunIdx>
  /** `run|instance` → the Task label (task.entered). */
  tasks: Record<string, string>
  /** `run|instance` → where that Task's current step group sits. */
  groups: Record<string, { msg: string; part: number }>
  /** Tool step ID → its row. */
  steps: Record<string, { msg: string; part: number; step: number }>
  /** Model step ID → its tool calls, so a tool row can show its arguments. */
  calls: Record<string, { name?: string; arguments?: unknown }[]>
  /** Saves per artifact slot (`kind[/key]`): the version is not in the payload. */
  slots: Record<string, number>
  /** Command ID → optimistic message ID. */
  optimistic: Record<string, string>
  /** The send_message command the next user.message answers. */
  nextUserCommand?: string
  /** input_id → message ID. */
  inputs: Record<string, string>
  /** The system message collecting an upload's document.stored facts, while it is the last one. */
  docs?: string
  /** Outbox key → the approval and run it belongs to. */
  outbox: Record<string, { run?: string; approval?: string; system?: string }>
  /** budget.reached facts by interrupt ID, for the budget card. */
  budgets: Record<string, { limit?: string; used?: number; allowed?: number }>
  /** Citations by carrier ref. */
  cites: Record<string, ArtifactCitation[]>
  /** A counter for local message IDs. */
  seq: number
}

export interface SessionStore {
  state: SessionState
  idx: Index
}

export function initialStore(sessionId: string | null): SessionStore {
  return {
    state: emptySessionState(sessionId),
    idx: { runs: {}, tasks: {}, groups: {}, steps: {}, calls: {}, slots: {}, optimistic: {}, inputs: {}, outbox: {}, budgets: {}, cites: {}, seq: 0 },
  }
}

/** Folds a batch of messages (the client delivers them batched). */
export function foldMessages(store: SessionStore, msgs: readonly StreamMessage[]): SessionStore {
  let s = store
  for (const m of msgs) s = sessionReducer(s, m)
  return s
}

/** THE reducer. Returns the same object when the message changes nothing. */
export function sessionReducer(store: SessionStore, msg: StreamMessage): SessionStore {
  switch (msg.kind) {
    case "synchronized": {
      const cursor = Math.max(store.state.cursor ?? -1, msg.seq)
      return { ...store, state: { ...store.state, phase: "ready", stream: "live", cursor } }
    }
    case "resync":
      // The server can no longer replay from our cursor: views refetch; the thread keeps what it has.
      return { ...store, state: { ...store.state, stream: "refetching", cursor: Math.max(store.state.cursor ?? -1, msg.seq) } }
    case "stream":
      return msg.state === store.state.stream ? store : { ...store, state: { ...store.state, stream: msg.state } }
    case "local":
      return local(store, msg.action)
    case "event": {
      if (typeof msg.seq !== "number") return store
      if (store.state.cursor != null && msg.seq <= store.state.cursor) return store
      if (!KNOWN.has(msg.type)) return { ...store, state: { ...store.state, cursor: msg.seq } }
      const next = fold(clone(store), msg)
      next.state.cursor = msg.seq
      return next
    }
    default:
      return store
  }
}

// ---- copy-on-write ------------------------------------------------------------------

/** A shallow working copy: arrays and records the fold may touch are copied once per event. */
function clone(store: SessionStore): SessionStore {
  const st = store.state
  return {
    state: { ...st, messages: [...st.messages], runs: [...st.runs], interrupts: { ...st.interrupts }, artifacts: [...st.artifacts] },
    idx: {
      ...store.idx,
      runs: { ...store.idx.runs }, tasks: { ...store.idx.tasks }, groups: { ...store.idx.groups }, steps: { ...store.idx.steps },
      calls: { ...store.idx.calls }, slots: { ...store.idx.slots }, optimistic: { ...store.idx.optimistic }, inputs: { ...store.idx.inputs },
      outbox: { ...store.idx.outbox }, budgets: { ...store.idx.budgets }, cites: { ...store.idx.cites },
    },
  }
}

const msgIndex = (s: SessionState, id: string) => s.messages.findIndex((m) => m.id === id)

function patchMessage(s: SessionState, id: string, fn: (m: Message) => Message) {
  const i = msgIndex(s, id)
  if (i >= 0) s.messages[i] = fn(s.messages[i])
}

function pushPart(s: SessionState, id: string, part: Part): number {
  let at = -1
  patchMessage(s, id, (m) => { at = m.parts.length; return { ...m, parts: [...m.parts, part] } })
  return at
}

function patchPartAt(s: SessionState, id: string, i: number, fn: (p: Part) => Part) {
  patchMessage(s, id, (m) => {
    if (!m.parts[i]) return m
    const parts = [...m.parts]
    parts[i] = fn(parts[i])
    return { ...m, parts }
  })
}

function setRun(s: SessionState, id: string, fn: (r: Run) => Run) {
  const i = s.runs.findIndex((r) => r.id === id)
  if (i >= 0) s.runs[i] = fn(s.runs[i])
}

// ---- small formatters -------------------------------------------------------------

const str = (v: unknown): string | undefined => (typeof v === "string" ? v : undefined)
const num = (v: unknown): number | undefined => (typeof v === "number" ? v : undefined)
const obj = (v: unknown): Record<string, unknown> => (v && typeof v === "object" && !Array.isArray(v) ? v as Record<string, unknown> : {})
const arr = (v: unknown): unknown[] => (Array.isArray(v) ? v : [])

/** "completion_checks_passed" → "completion checks passed". */
export const humanize = (s: string) => s.replace(/[_-]+/g, " ").trim()
const sentence = (s: string) => (s ? s[0].toUpperCase() + s.slice(1) : s)

/** "candidate[C2]/extract" → "Candidate C2 · extract". */
export function taskLabel(instance: string): string {
  return sentence(instance.split("/").map((p) => p.replace(/\[([^\]]+)\]/g, " $1")).join(" · "))
}

export function fmtElapsed(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) return "0s"
  if (ms < 10_000) return `${(ms / 1000).toFixed(1)}s`
  const s = Math.round(ms / 1000)
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`
}

const fmtCount = (n: number) => (n < 1000 ? String(n) : n < 1e6 ? `${(n / 1000).toFixed(1)}k` : `${(n / 1e6).toFixed(1)}M`)

/** The view kind a media type opens as. */
export function kindForMedia(media?: string, path?: string): ViewKind {
  const m = (media ?? "").toLowerCase()
  const ext = (path ?? "").toLowerCase().split(".").pop() ?? ""
  if (m === "text/csv" || ext === "csv") return "table"
  if (m === "text/markdown" || ext === "md") return "markdown"
  if (m.includes("json") || ext === "json") return "json"
  if (m === "application/pdf" || ext === "pdf") return "pdf"
  if (m.startsWith("image/")) return "image"
  if (m === "text/html" || ext === "html") return "html"
  return "text"
}

export const artifactViewId = (ref: string) => `artifact:${ref}`
export const documentViewId = (path: string, version: number) => `input:${path}@v${version}`

/** A ViewRef for an artifact version ref (`kind[/key]@vN`), e.g. from a deep link. */
export function artifactView(ref: string, extra: Partial<ViewRef> = {}): ViewRef {
  const [slot, v] = ref.split("@")
  const n = Number((v ?? "").replace(/^v/, ""))
  const versions = n > 0 ? Array.from({ length: n }, (_, i) => `v${i + 1}`) : undefined
  return { id: artifactViewId(ref), kind: "json", title: slot, source: "artifact", ref, version: v, versions, ...extra }
}

const runStatus: Record<string, { status: Status; label: string; tone: Status; text: string }> = {
  complete: { status: "ok", label: "Complete", tone: "ok", text: "Run complete" },
  partial: { status: "wait", label: "Partial", tone: "wait", text: "Finished with a partial result" },
  blocked: { status: "bad", label: "Blocked", tone: "bad", text: "Run blocked" },
  stopped: { status: "idle", label: "Stopped", tone: "idle", text: "Run stopped" },
  failed: { status: "bad", label: "Failed", tone: "bad", text: "Run failed" },
}

// ---- thread placement -------------------------------------------------------------

function newId(idx: Index, prefix: string) {
  idx.seq += 1
  return `${prefix}:${idx.seq}`
}

/** The run's current agent message: reused while it is the last message, else a new one. */
function segmentFor(st: SessionStore, run: string, at: string): string {
  const { state: s, idx } = st
  const r = idx.runs[run] ?? { agent: "agent", turns: 0, tokens: 0, segments: 0 }
  const last = s.messages.at(-1)
  if (r.segment && last?.id === r.segment) return r.segment
  const segments = r.segments + 1
  const id = `run:${run}:${segments}`
  const parts: Part[] = []
  if (r.lead && segments === 1) parts.push({ type: "notice", tone: "idle", text: r.lead })
  s.messages.push({ id, role: "agent", author: r.agent, at, parts })
  idx.runs[run] = { ...r, segments, segment: id }
  return id
}

/** The step group for a Task: reused while it is the last part of the run's message. */
function groupFor(st: SessionStore, run: string, instance: string, at: string): { msg: string; part: number } {
  const key = `${run}|${instance}`
  const msg = segmentFor(st, run, at)
  const g = st.idx.groups[key]
  const m = st.state.messages[msgIndex(st.state, msg)]
  if (g && g.msg === msg && g.part === m.parts.length - 1) return g
  const label = st.idx.tasks[key] ?? taskLabel(instance)
  const part = pushPart(st.state, msg, { type: "step-group", label, steps: [] })
  const loc = { msg, part }
  st.idx.groups[key] = loc
  return loc
}

/** Updates every place a view with this ID appears (chips, group saves, tool rows, the list). */
function mapView(s: SessionState, id: string, fn: (v: ViewRef) => ViewRef) {
  const one = (p: Part): Part => {
    switch (p.type) {
      case "artifact": return p.view.id === id ? { ...p, view: fn(p.view) } : p
      case "tool": return p.view?.id === id ? { ...p, view: fn(p.view) } : p
      case "step-group": {
        const touches = p.saved?.some((v) => v.id === id) || p.steps.some((t) => t.view?.id === id)
        return touches ? { ...p, saved: p.saved?.map((v) => (v.id === id ? fn(v) : v)), steps: p.steps.map((t) => one(t) as ToolPart) } : p
      }
      default: return p
    }
  }
  s.messages = s.messages.map((m) => {
    const parts = m.parts.map(one)
    return parts.some((p, i) => p !== m.parts[i]) ? { ...m, parts } : m
  })
  s.artifacts = s.artifacts.map((v) => (v.id === id ? fn(v) : v))
}

/** A session has begun once it has a message; owner-wide facts before that are not its story. */
const started = (s: SessionState) => s.messages.some((m) => m.role === "user" || m.role === "agent")

function systemMessage(st: SessionStore, at: string, parts: Part[]): string {
  const id = newId(st.idx, "sys")
  st.state.messages.push({ id, role: "system", author: "Aksha", at, parts })
  return id
}

function touchRun(st: SessionStore, run: string | null | undefined, at: string | undefined) {
  if (!run || !at) return
  const r = st.idx.runs[run]
  if (!r) return
  st.idx.runs[run] = { ...r, lastAt: at }
  const elapsed = r.startedAt ? Date.parse(at) - Date.parse(r.startedAt) : 0
  setRun(st.state, run, (x) => ({ ...x, elapsed: fmtElapsed(elapsed) }))
}

function openInterrupts(s: SessionState, run: string) {
  return Object.values(s.interrupts).some((i) => i.runId === run && (i.state === "open" || i.state === "submitting" || i.state === "refused"))
}

function afterInterruptClosed(st: SessionStore, run?: string) {
  if (!run || st.idx.runs[run]?.ended) return
  if (!openInterrupts(st.state, run)) setRun(st.state, run, (r) => (r.status === "wait" ? { ...r, status: "run", label: "Running" } : r))
}

// ---- the fold -----------------------------------------------------------------------

function fold(st: SessionStore, e: WireEvent): SessionStore {
  const s = st.state
  const idx = st.idx
  const p = obj(e.payload)
  const at = e.at ?? ""
  const run = e.run ?? undefined
  touchRun(st, run, at)

  switch (e.type) {
    case "command.accepted": {
      const cid = str(p.command_id)
      if (cid && idx.optimistic[cid] && str(p.name) === "send_message") idx.nextUserCommand = cid
      return st
    }
    case "command.refused": {
      const cid = str(p.command_id)
      if (cid && idx.optimistic[cid]) return dropOptimistic(st, cid)
      return st
    }

    case "user.message": {
      const text = str(p.text) ?? ""
      let id = idx.nextUserCommand ? idx.optimistic[idx.nextUserCommand] : undefined
      if (!id) id = s.messages.find((m) => m.pending && m.role === "user" && !m.lane && textOf(m) === text)?.id
      idx.nextUserCommand = undefined
      for (const [k, v] of Object.entries(idx.optimistic)) if (v === id) delete idx.optimistic[k]
      if (id && msgIndex(s, id) >= 0) {
        patchMessage(s, id, (m) => ({ ...m, at, pending: false }))
      } else {
        s.messages.push({ id: `msg:${str(p.message) ?? e.seq}`, role: "user", author: "You", at, parts: [{ type: "text", text }] })
      }
      if (s.title === s.sessionId || s.title === "New chat" || !s.title) s.title = text.length > 80 ? `${text.slice(0, 79)}…` : text
      if (!s.agent && str(p.agent)) s.agent = str(p.agent)
      idx.docs = undefined
      return st
    }

    case "route.decided": {
      const r = str(p.run)
      const agent = str(p.agent) ?? "agent"
      if (!s.agent) s.agent = agent
      if (r) {
        const probs = obj(p.probabilities_ppm)
        const ppm = num(probs[agent])
        const lead = str(p.by) === "classifier"
          ? `Routed to ${agent}${ppm != null ? ` (${Math.round(ppm / 10_000)}% match)` : ""}`
          : undefined
        idx.runs[r] = { ...(idx.runs[r] ?? { turns: 0, tokens: 0, segments: 0 }), agent, lead }
      }
      return st
    }

    case "run.started": {
      if (!run) return st
      const agent = str(p.agent) ?? idx.runs[run]?.agent ?? "agent"
      const forRuns = arr(p.for_runs).map(String)
      const prev = idx.runs[run]
      const lead = forRuns.length
        ? `Rebuilding after an input change: this run reworks ${forRuns.join(", ")}`
        : prev?.lead
      idx.runs[run] = { ...(prev ?? { turns: 0, tokens: 0, segments: 0 }), agent, startedAt: at, lastAt: at, lead }
      if (!s.runs.some((r) => r.id === run)) {
        s.runs.push({ id: run, agent, status: "run", label: forRuns.length ? "Rebuilding" : "Running", turns: "0", tokens: "0", elapsed: "0s", forRuns: forRuns.length ? forRuns : undefined })
      }
      if (lead) segmentFor(st, run, at)
      return st
    }

    case "task.entered": {
      if (run && e.instance) idx.tasks[`${run}|${e.instance}`] = taskLabel(e.instance)
      return st
    }

    case "step.started":
    case "step.completed": {
      if (!run || !e.step) return st // the router's own model step belongs to no run
      if (p.kind === "model") return modelStep(st, e, p, run)
      if (p.kind === "tool") return toolStep(st, e, p, run)
      return st
    }

    case "artifact.saved": {
      const kind = str(p.kind)
      if (!kind) return st
      const key = str(p.key)
      const slot = key ? `${kind}/${key}` : kind
      const n = (idx.slots[slot] ?? 0) + 1
      idx.slots[slot] = n
      const ref = `${slot}@v${n}`
      const view = artifactView(ref, { citations: idx.cites[ref] })
      s.artifacts = [...s.artifacts.filter((v) => !(v.source === "artifact" && v.title === slot)), view]
      s.suggestedView = view.id
      if (!run) return st
      const step = e.step ? idx.steps[e.step] : undefined
      if (step) {
        patchPartAt(s, step.msg, step.part, (g) => g.type !== "step-group" ? g : {
          ...g, steps: g.steps.map((t, i) => (i === step.step ? { ...t, view } : t)),
        })
      }
      const g = e.instance ? idx.groups[`${run}|${e.instance}`] : undefined
      if (g && msgIndex(s, g.msg) >= 0) {
        patchPartAt(s, g.msg, g.part, (x) => x.type === "step-group" ? { ...x, saved: [...(x.saved ?? []), view] } : x)
      } else {
        pushPart(s, segmentFor(st, run, at), { type: "artifact", view, note: e.instance ? `Saved by ${taskLabel(e.instance)}` : "Saved" })
      }
      return st
    }

    case "citation.bound": {
      const carrier = str(p.carrier)
      if (!carrier) return st
      const c: ArtifactCitation = { pointer: str(p.pointer), target: str(p.target), quote: str(p.quote), form: str(p.form) }
      const list = [...(idx.cites[carrier] ?? []), c]
      idx.cites[carrier] = list
      mapView(s, artifactViewId(carrier), (v) => ({ ...v, citations: list }))
      return st
    }

    case "budget.reached": {
      const id = str(p.interrupt_id)
      if (id) idx.budgets[id] = { limit: str(p.limit), used: num(p.used), allowed: num(p.allowed) }
      return st
    }

    case "interrupt.raised":
    case "approval.requested":
      return raised(st, e, p)

    case "interrupt.resolved": {
      const id = str(p.interrupt)
      const i = id ? s.interrupts[id] : undefined
      if (!i || !id) return st
      if (i.kind === "approval") {
        const d = str(p.decision)
        if (d === "granted" && i.state !== "granted") s.interrupts[id] = { ...i, state: "granted" }
        if (d === "rejected" && i.state !== "rejected") s.interrupts[id] = { ...i, state: "rejected" }
      } else {
        const answer = i.kind === "review" && i.state === "resolved" && i.answer ? i.answer : answerText(i, p)
        s.interrupts[id] = { ...i, state: "resolved", answer, decidedBy: by(p), refusal: undefined }
      }
      afterInterruptClosed(st, i.runId)
      return st
    }

    case "review.accepted":
    case "review.rejected": {
      // Recorded just before the review's interrupt.resolved: the decision its closed line shows.
      const id = str(p.interrupt)
      const i = id ? s.interrupts[id] : undefined
      if (!i || !id || !i.review) return st
      const answer = e.type === "review.accepted" ? `accepted ${i.review.to}` : `kept ${i.review.from}`
      s.interrupts[id] = { ...i, state: "resolved", answer, refusal: undefined }
      afterInterruptClosed(st, i.runId)
      return st
    }

    case "interrupt.voided":
    case "approval.voided": {
      const id = str(p.interrupt) ?? str(p.approval)
      const i = id ? s.interrupts[id] : undefined
      if (!i || !id) return st
      const reason = str(p.reason)
      s.interrupts[id] = { ...i, state: "voided", detail: reason ? `${sentence(humanize(reason))}.` : undefined }
      afterInterruptClosed(st, i.runId)
      return st
    }

    case "approval.granted":
    case "approval.rejected": {
      const id = str(p.approval)
      const i = id ? s.interrupts[id] : undefined
      if (!i || !id) return st
      const granted = e.type === "approval.granted"
      s.interrupts[id] = { ...i, state: granted ? "granted" : "rejected", decidedBy: by(p), refusal: undefined, detail: granted ? i.detail : str(p.note) ?? i.detail }
      afterInterruptClosed(st, i.runId)
      return st
    }

    case "budget.granted": {
      const id = str(p.interrupt) ?? str(p.interrupt_id)
      const i = id ? s.interrupts[id] : undefined
      if (i && id) s.interrupts[id] = { ...i, state: "resolved", answer: "More budget granted", decidedBy: by(p) }
      afterInterruptClosed(st, i?.runId)
      return st
    }

    case "input.queued": {
      const cid = str(p.command_id)
      const inputId = str(p.input_id) ?? String(e.seq)
      const lane = (str(p.lane) ?? "steer") as Message["lane"]
      const opt = cid ? idx.optimistic[cid] : undefined
      if (opt && msgIndex(s, opt) >= 0) {
        patchMessage(s, opt, (m) => ({ ...m, pending: false, lane, laneState: "queued", at }))
        delete idx.optimistic[cid!]
        idx.inputs[inputId] = opt
      } else {
        const id = `in:${inputId}`
        s.messages.push({ id, role: "user", author: "You", at, lane, laneState: "queued", parts: [{ type: "text", text: str(p.text) ?? "" }] })
        idx.inputs[inputId] = id
      }
      return st
    }

    case "input.consumed": {
      const m = idx.inputs[str(p.input_id) ?? ""]
      if (m) patchMessage(s, m, (x) => ({ ...x, laneState: "consumed" }))
      return st
    }

    case "run.stop_requested": {
      if (run) setRun(s, run, (r) => ({ ...r, label: "Stop requested" }))
      return st
    }
    case "run.finalizing": {
      if (run) setRun(s, run, (r) => ({ ...r, label: "Finishing" }))
      return st
    }

    case "run.ended": {
      if (!run) return st
      const outcome = str(p.outcome) ?? "complete"
      const m = runStatus[outcome] ?? { status: "idle" as Status, label: sentence(humanize(outcome)), tone: "idle" as Status, text: `Run ${humanize(outcome)}` }
      const reason = str(p.reason)
      idx.runs[run] = { ...(idx.runs[run] ?? { agent: "agent", turns: 0, tokens: 0, segments: 0 }), ended: true }
      setRun(s, run, (r) => ({ ...r, status: m.status, label: m.label, outcome }))
      pushPart(s, segmentFor(st, run, at), { type: "notice", tone: m.tone, text: reason ? `${m.text}: ${humanize(reason)}.` : `${m.text}.` })
      return st
    }

    case "document.stored": {
      const path = str(p.path)
      const version = num(p.version) ?? 1
      if (!path) return st
      const media = str(p.detected_type) ?? str(p.declared_type) ?? undefined
      const view: ViewRef = {
        id: documentViewId(path, version), kind: kindForMedia(media, path), title: path, source: "input",
        version: `v${version}`, versions: Array.from({ length: version }, (_, i) => `v${i + 1}`),
        docKey: str(p.doc_key), mediaType: media,
      }
      s.artifacts = [...s.artifacts.filter((v) => !(v.source === "input" && v.title === path)), view]
      if (!started(s)) return st
      const last = s.messages.at(-1)
      let msg = idx.docs && last?.id === idx.docs ? idx.docs : undefined
      if (!msg) {
        msg = systemMessage(st, at, [{ type: "files", files: [] }])
        idx.docs = msg
      }
      patchPartAt(s, msg, 0, (x) => (x.type === "files" ? { ...x, files: [...x.files.filter((f) => f.title !== path), view] } : x))
      // A changed file says so on its card (v1 → v2); the source.changed notice that follows names them together.
      return st
    }

    case "source.changed": {
      if (!started(s)) return st
      const paths = [...new Set(Object.keys(obj(p.records)).map((k) => k.split("#")[0].replace(/^input\//, "")))]
      if (!paths.length) return st
      const text = `Inputs changed: ${paths.join(", ")}. Work built on them is rebuilt.`
      const last = s.messages.at(-1)
      if (idx.docs && last?.id === idx.docs) pushPart(s, idx.docs, { type: "notice", tone: "wait", text })
      else systemMessage(st, at, [{ type: "notice", tone: "wait", text }])
      return st
    }

    case "outbox.enqueued": {
      const key = str(p.key)
      if (key) idx.outbox[key] = { run, approval: str(p.approval_id), system: str(p.system) }
      return st
    }
    case "outbox.completed":
    case "outbox.abandoned": {
      const o = idx.outbox[str(p.key) ?? ""]
      if (!o?.run) return st
      const ok = e.type === "outbox.completed"
      const text = ok ? `Sent to ${o.system ?? "the outside system"}.` : `Could not send to ${o.system ?? "the outside system"}; nothing was delivered.`
      if (o.approval && s.interrupts[o.approval]) {
        const i = s.interrupts[o.approval]
        if (i.action) s.interrupts[o.approval] = { ...i, action: { ...i.action, delivered: ok ? `sent to ${o.system ?? "the outside system"}` : undefined } }
      }
      pushPart(s, segmentFor(st, o.run, at), { type: "notice", tone: ok ? "ok" : "bad", text })
      return st
    }

    default:
      // Bookkeeping (stop.decided, completion_check.evaluated, read.recorded, excerpts.*,
      // query.reexecuted, outbox.claimed) and unknown types: nothing visible.
      return st
  }
}

const by = (p: Record<string, unknown>) => {
  const b = str(p.by)
  return b && b !== "person" ? b : undefined
}

const textOf = (m: Message) => m.parts.map((p) => (p.type === "text" ? p.text : "")).join("")

function answerText(i: Interrupt, p: Record<string, unknown>): string {
  const answers = obj(p.answers)
  const vals = Object.entries(answers).map(([q, v]) => {
    const s = typeof v === "string" ? v : JSON.stringify(v)
    const opt = i.questions?.find((x) => x.id === q)?.options?.find((o) => o.key === s)
    return opt && opt.hint ? `${opt.label} (${opt.hint})` : s
  })
  if (vals.length) return vals.join("; ")
  return str(p.agent) ?? str(p.text) ?? str(p.decision) ?? "answered"
}

function modelStep(st: SessionStore, e: WireEvent, p: Record<string, unknown>, run: string): SessionStore {
  if (e.type === "step.started") return st
  const result = obj(obj(p.value).result)
  st.idx.calls[e.step!] = arr(result.tool_calls).map((c) => ({ name: str(obj(c).name), arguments: obj(c).arguments }))
  const usage = obj(p.usage)
  const prev = st.idx.runs[run]
  if (prev) {
    const r = { ...prev, turns: prev.turns + 1, tokens: prev.tokens + (num(usage.input_tokens) ?? 0) + (num(usage.output_tokens) ?? 0) }
    st.idx.runs[run] = r
    setRun(st.state, run, (x) => ({ ...x, turns: String(r.turns), tokens: fmtCount(r.tokens) }))
  }
  const text = str(result.text)
  if (text && text.trim()) pushPart(st.state, segmentFor(st, run, e.at ?? ""), { type: "text", text })
  return st
}

function argSummary(args: unknown): string {
  const a = obj(args)
  const first = Object.values(a).find((v) => typeof v === "string") as string | undefined
  const s = first ?? (Object.keys(a).length ? JSON.stringify(a) : "")
  return s.length > 90 ? `${s.slice(0, 89)}…` : s
}

function parseObj(raw?: string): Record<string, unknown> {
  if (!raw) return {}
  try { return obj(JSON.parse(raw)) } catch { return {} }
}

function toolStep(st: SessionStore, e: WireEvent, p: Record<string, unknown>, run: string): SessionStore {
  const s = st.state
  const stepId = e.step!
  const name = str(p.tool) ?? str(p.name) ?? "tool"
  let loc = st.idx.steps[stepId]
  if (!loc) {
    // R/inst/tNN/cMM: call MM of the model step R/inst/tNN.
    const m = stepId.match(/^(.*)\/c(\d+)$/)
    const call = m ? st.idx.calls[m[1]]?.[Number(m[2]) - 1] : undefined
    const g = groupFor(st, run, e.instance ?? "run", e.at ?? "")
    let stepIndex = -1
    patchPartAt(s, g.msg, g.part, (x) => {
      if (x.type !== "step-group") return x
      stepIndex = x.steps.length
      const row: ToolPart = {
        type: "tool", name, state: "running", summary: argSummary(call?.arguments),
        input: call?.arguments !== undefined ? JSON.stringify(call.arguments) : undefined,
      }
      return { ...x, steps: [...x.steps, row] }
    })
    loc = { msg: g.msg, part: g.part, step: stepIndex }
    st.idx.steps[stepId] = loc
  }
  if (e.type === "step.completed") {
    const view = obj(obj(p.value).view)
    const raw = str(view.text)
    const detail = parseObj(raw)
    const outcome = str(p.outcome) ?? "ok"
    const code = str(p.code) ?? str(detail.code)
    const why = str(detail.message)
    const state: ToolPart["state"] = outcome === "ok" ? "done" : outcome === "refused" || str(view.status) === "refused" ? "refused" : "error"
    const refusal = state === "done" ? undefined : [code, why].filter(Boolean).join(": ") || outcome
    const output = raw ? (raw.length > 600 ? `${raw.slice(0, 600)}…` : raw) : undefined
    patchPartAt(s, loc.msg, loc.part, (x) => x.type !== "step-group" ? x : {
      ...x, steps: x.steps.map((t, i) => (i === loc.step ? { ...t, state, durationMs: num(p.duration_ms), refusal, output: state === "done" ? output : undefined } : t)),
    })
  }
  return st
}

/** "Which … ? C1: prioritize_counsel_review; C2: …" → hints per option key. */
function optionHints(text: string, keys: string[]): Record<string, string> {
  const out: Record<string, string> = {}
  for (const k of keys) {
    const m = text.match(new RegExp(`(?:^|[\\s;?])${k.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}:\\s*([^;]+)`))
    if (m) out[k] = humanize(m[1].trim())
  }
  return out
}

function raised(st: SessionStore, e: WireEvent, p: Record<string, unknown>): SessionStore {
  const s = st.state
  const id = str(p.interrupt) ?? str(p.approval)
  if (!id) return st
  const kind = (e.type === "approval.requested" ? "approval" : str(p.kind) ?? "input") as Interrupt["kind"]
  const prev = s.interrupts[id]
  const run = e.run ?? undefined
  const questions = arr(p.questions).map((q) => {
    const o = obj(q)
    return { id: str(o.id) ?? "q1", text: str(o.text) ?? "" }
  })
  let next: Interrupt
  switch (kind) {
    case "approval": {
      const body = obj(p.body)
      const facts = Object.entries(obj(body.facts ?? p.facts)).map(([k, v]) => [k, typeof v === "string" ? v : JSON.stringify(v)] as [string, string])
      const tool = str(body.tool) ?? str(p.tool) ?? prev?.action?.tool ?? "action"
      const targets = arr(p.targets).map(String)
      next = {
        ...prev, id, runId: run ?? prev?.runId, kind, state: prev?.state ?? "open",
        title: sentence(humanize(tool)),
        action: {
          ...prev?.action, tool, leavesSystem: true,
          facts: facts.length ? facts : prev?.action?.facts ?? [],
          signature: str(p.signature) ?? prev?.action?.signature ?? "",
          targets: targets.length ? targets : prev?.action?.targets,
        },
      }
      break
    }
    case "choice":
    case "route": {
      const keys = arr(p.options).map(String)
      const text = questions[0]?.text ?? ""
      const purposes = obj(p.purposes)
      const hints = kind === "route" ? Object.fromEntries(keys.map((k) => [k, str(purposes[k]) ?? ""])) : optionHints(text, keys)
      const shown = kind === "choice" ? text.replace(/\s*[A-Za-z0-9_-]+:\s*[^;]+(;|$)/g, "").trim() || text : text
      next = {
        id, runId: run, kind, state: "open",
        title: kind === "route" ? "Which agent should take this?" : sentence(humanize(str(p.label) ?? "choose one")),
        questions: [{ id: questions[0]?.id ?? "q1", text: shown, options: keys.map((k) => ({ key: k, label: k, hint: hints[k] || undefined })) }],
      }
      break
    }
    case "budget": {
      const b = st.idx.budgets[id] ?? {}
      const dim = b.limit ?? str(p.reason) ?? "budget"
      next = {
        id, runId: run, kind, state: "open", title: `The run spent its ${humanize(dim)} budget`,
        detail: "Grant more to keep going, finish now with what it has, or stop.",
        budget: { used: String(b.used ?? "?"), limit: String(b.allowed ?? "?"), options: ["grant", "finalize", "stop"], dimension: dim, allowed: b.allowed },
      }
      break
    }
    case "review": {
      const old = str(p.old) ?? ""
      const revision = str(p.revision) ?? ""
      const [slot, from] = old.split("@")
      const to = revision.split("@")[1] ?? ""
      const hunks = arr(p.hunks).map(obj)
      const fmt = (v: unknown) => (typeof v === "string" ? v : JSON.stringify(v, null, 2))
      const view: ViewRef = {
        id: `review:${id}`, kind: "diff", title: `${slot} ${from} → ${to}`, source: "artifact", ref: revision,
        diff: {
          mode: "text", label: slot,
          before: hunks.map((h) => `${str(h.path) ?? ""}: ${fmt(h.old)}`).join("\n"),
          after: hunks.map((h) => `${str(h.path) ?? ""}: ${fmt(h.new)}`).join("\n"),
        },
      }
      next = {
        id, runId: run, kind, state: "open", title: `Review the rebuilt ${slot}`,
        review: { artifact: slot, from, to, changes: hunks.length, view, cardHash: str(p.card_hash), old, revision },
      }
      break
    }
    default:
      next = {
        id, runId: run, kind: "input", state: "open",
        title: sentence(humanize(str(p.label) ?? "question")),
        questions: questions.map((q) => ({ ...q, freeText: true })),
      }
  }
  s.interrupts[id] = next
  if (!prev) {
    if (run) {
      pushPart(s, segmentFor(st, run, e.at ?? ""), { type: "interrupt", interruptId: id })
      setRun(s, run, (r) => ({ ...r, status: "wait", label: "Waiting on you" }))
    } else {
      const mid = newId(st.idx, "ask")
      s.messages.push({ id: mid, role: "agent", author: "Aksha", at: e.at ?? "", parts: [{ type: "interrupt", interruptId: id }] })
    }
  }
  return st
}

// ---- local actions ------------------------------------------------------------------

function dropOptimistic(st: SessionStore, cid: string): SessionStore {
  const id = st.idx.optimistic[cid]
  const optimistic = { ...st.idx.optimistic }
  delete optimistic[cid]
  return {
    state: { ...st.state, messages: st.state.messages.filter((m) => m.id !== id || !m.pending) },
    idx: { ...st.idx, optimistic, nextUserCommand: st.idx.nextUserCommand === cid ? undefined : st.idx.nextUserCommand },
  }
}

function local(store: SessionStore, a: LocalAction): SessionStore {
  switch (a.type) {
    case "optimistic-message": {
      if (store.idx.optimistic[a.commandId]) return store
      const id = `opt:${a.commandId}`
      const parts: Part[] = [{ type: "text", text: a.text }]
      if (a.files?.length) parts.push({ type: "files", files: a.files })
      const m: Message = { id, role: "user", author: a.author ?? "You", at: a.at, parts, pending: true, lane: a.lane, laneState: a.lane ? "queued" : undefined }
      const s = store.state
      return {
        state: {
          ...s, messages: [...s.messages, m],
          title: !s.messages.length && (s.title === s.sessionId || s.title === "New chat") ? (a.text.length > 80 ? `${a.text.slice(0, 79)}…` : a.text) : s.title,
          agent: s.agent ?? a.agent,
        },
        idx: { ...store.idx, optimistic: { ...store.idx.optimistic, [a.commandId]: id } },
      }
    }
    case "command-refused":
      return store.idx.optimistic[a.commandId] ? dropOptimistic(store, a.commandId) : store
    case "submitting": {
      const i = store.state.interrupts[a.interruptId]
      if (!i) return store
      const state = a.on ? (i.state === "open" || i.state === "refused" ? "submitting" : i.state) : i.state === "submitting" ? "open" : i.state
      return state === i.state ? store : { ...store, state: { ...store.state, interrupts: { ...store.state.interrupts, [i.id]: { ...i, state } } } }
    }
    case "uploaded": {
      const paths = new Set(a.files.map((f) => f.title))
      const artifacts = [...store.state.artifacts.filter((v) => !(v.source === "input" && paths.has(v.title))), ...a.files]
      return { ...store, state: { ...store.state, artifacts } }
    }
    case "failed":
      return { ...store, state: { ...store.state, phase: a.code === "not_found" ? "not_found" : "error", error: { code: a.code, message: a.message } } }
  }
}
