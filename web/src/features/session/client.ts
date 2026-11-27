// SessionClient: one live connection per open session to GET /sessions/{s}/events.
//
// It tracks the cursor (the last seq seen), resumes from it after any drop with backoff,
// watches heartbeats (a stream quiet for over 30 s counts as dropped), and hands batches of
// stream messages to the reducer. The M6 stream state travels with the batch as
// `{kind: "stream"}` messages; `synchronized` (the reducer turns it into Live) and `resync`
// pass through as their own messages.
//
// The connection is anything EventSource-shaped. The default reads the stream with `fetch`,
// because a browser EventSource cannot see the `: heartbeat` comment lines or deliver event
// types it has no listener for; the fetch reader dispatches comments as "heartbeat" and
// unlistened types as "message", so unknown events still advance the cursor.

import { sessionEventsUrl } from "@/api"
import { KNOWN_EVENT_TYPES, type StreamMessage } from "./wire"
import type { StreamState } from "./types"

/** One SSE block as a listener sees it (the MessageEvent fields we use). */
export interface SseEvent { type: string; data: string; lastEventId: string }

/** The part of the EventSource interface the client uses. */
export interface EventSourceLike {
  onopen: ((ev: unknown) => void) | null
  onerror: ((ev: unknown) => void) | null
  addEventListener(type: string, listener: (ev: SseEvent) => void): void
  close(): void
}

export type Connect = (url: string) => EventSourceLike

export interface SessionClientOptions {
  sessionId: string
  /** Resume after this seq (a reconnect, or a store that already folded a replay). */
  cursor?: number | null
  onMessages: (batch: StreamMessage[]) => void
  /** Called on `resync`: refetch the views; the client returns to Live when it resolves. */
  onResync?: (seq: number) => void | Promise<void>
  connect?: Connect
  /** A stream with no event or heartbeat for this long counts as dropped (M6: 30 s). */
  heartbeatMs?: number
  /** Delay before reconnect attempt n (1-based). */
  backoff?: (attempt: number) => number
}

export const defaultBackoff = (n: number) => Math.min(15_000, 500 * 2 ** (n - 1))

/** Every type the client listens for on a connection. */
const LISTEN = ["synchronized", "resync", "message", "heartbeat", ...KNOWN_EVENT_TYPES]

export class SessionClient {
  readonly sessionId: string
  private cursor: number | null
  private source: EventSourceLike | null = null
  private attempts = 0
  private state: StreamState = "connecting"
  private closed = false
  private watchdog: ReturnType<typeof setTimeout> | null = null
  private retry: ReturnType<typeof setTimeout> | null = null
  private buffer: StreamMessage[] = []
  private flushQueued = false
  private readonly opts: Required<Pick<SessionClientOptions, "heartbeatMs" | "backoff" | "connect">> & SessionClientOptions

  constructor(opts: SessionClientOptions) {
    this.sessionId = opts.sessionId
    this.cursor = opts.cursor ?? null
    this.opts = { heartbeatMs: 30_000, backoff: defaultBackoff, connect: fetchEventSource, ...opts }
  }

  /** The last seq seen: the next connection asks for everything after it. */
  get position() { return this.cursor }
  get streamState() { return this.state }

  start() {
    this.closed = false
    this.setState(this.attempts ? "reconnecting" : "connecting")
    this.open()
  }

  close() {
    this.closed = true
    this.source?.close()
    this.source = null
    this.clearTimers()
    // Nothing reaches the store after close: a closed session's late events would land in the next one.
    this.buffer = []
    this.flushQueued = false
  }

  /** Delivers buffered messages now (normally done once per microtask). */
  flush() {
    this.flushQueued = false
    if (!this.buffer.length) return
    const batch = this.buffer
    this.buffer = []
    this.opts.onMessages(batch)
  }

  private emit(m: StreamMessage) {
    this.buffer.push(m)
    if (!this.flushQueued) {
      this.flushQueued = true
      queueMicrotask(() => { if (this.flushQueued) this.flush() })
    }
  }

  private setState(s: StreamState) {
    if (this.state === s) return
    this.state = s
    this.emit({ kind: "stream", state: s })
  }

  private url() {
    return sessionEventsUrl(this.sessionId, this.cursor ?? undefined)
  }

  private open() {
    const src = this.opts.connect(this.url())
    this.source = src
    src.onopen = () => {
      if (src !== this.source) return
      this.alive()
      this.setState("replaying")
    }
    src.onerror = (ev) => {
      if (src !== this.source) return
      const status = (ev as { status?: number } | null)?.status
      if (status === 404 || status === 401 || status === 403) {
        this.emit({ kind: "local", action: { type: "failed", code: status === 404 ? "not_found" : "forbidden", message: status === 404 ? "This session does not exist." : "You cannot read this session." } })
        this.flush()
        this.close()
        return
      }
      this.drop()
    }
    const on = (ev: SseEvent) => { if (src === this.source) this.receive(ev) }
    for (const t of LISTEN) src.addEventListener(t, on)
    this.alive()
  }

  /** Any sign of life resets the heartbeat watchdog. */
  private alive() {
    if (this.watchdog) clearTimeout(this.watchdog)
    this.watchdog = setTimeout(() => this.drop(), this.opts.heartbeatMs)
  }

  private drop() {
    if (this.closed) return
    this.source?.close()
    this.source = null
    this.clearTimers()
    this.attempts += 1
    this.setState("reconnecting")
    this.retry = setTimeout(() => { this.retry = null; if (!this.closed) this.open() }, this.opts.backoff(this.attempts))
  }

  private clearTimers() {
    if (this.watchdog) clearTimeout(this.watchdog)
    if (this.retry) clearTimeout(this.retry)
    this.watchdog = this.retry = null
  }

  private advance(seq: number | undefined) {
    if (seq == null || !Number.isFinite(seq)) return
    this.cursor = this.cursor == null ? seq : Math.max(this.cursor, seq)
  }

  private receive(ev: SseEvent) {
    this.alive()
    if (ev.type === "heartbeat") return
    const idSeq = ev.lastEventId !== "" && ev.lastEventId != null ? Number(ev.lastEventId) : undefined
    let data: Record<string, unknown> | undefined
    try { data = JSON.parse(ev.data) as Record<string, unknown> } catch { data = undefined }
    const seq = typeof data?.seq === "number" ? data.seq : idSeq
    if (!data) { this.advance(seq); return }

    if (data.kind === "synchronized" || ev.type === "synchronized") {
      this.advance(seq)
      this.attempts = 0
      this.state = "live" // the reducer sets Live from the marker itself
      this.emit({ kind: "synchronized", seq: seq ?? this.cursor ?? 0 })
      return
    }
    if (data.kind === "resync" || ev.type === "resync") {
      this.advance(seq)
      this.state = "refetching"
      this.emit({ kind: "resync", seq: seq ?? this.cursor ?? 0 })
      void Promise.resolve(this.opts.onResync?.(seq ?? 0)).finally(() => { if (!this.closed && this.state === "refetching") this.setState("live") })
      return
    }
    if (typeof seq === "number" && this.cursor != null && seq <= this.cursor) return // already seen
    this.advance(seq)
    if (data.kind === "event" && typeof data.type === "string") this.emit(data as unknown as StreamMessage)
    // anything else: skipped, but the cursor moved past it
  }
}

// ---- the default connection: SSE over fetch ----------------------------------------

type Listener = (ev: SseEvent) => void

/** An EventSource-shaped reader over `fetch` (credentials included). */
export function fetchEventSource(url: string): EventSourceLike {
  const listeners = new Map<string, Listener[]>()
  const ctl = new AbortController()
  const es: EventSourceLike = {
    onopen: null,
    onerror: null,
    addEventListener(type, fn) { listeners.set(type, [...(listeners.get(type) ?? []), fn]) },
    close() { ctl.abort() },
  }
  const dispatch = (type: string, data: string, lastEventId: string) => {
    const ls = listeners.get(type) ?? (type === "heartbeat" ? [] : listeners.get("message") ?? [])
    for (const l of ls) l({ type, data, lastEventId })
  }
  void (async () => {
    let res: Response
    try {
      res = await fetch(url, { credentials: "include", headers: { Accept: "text/event-stream" }, signal: ctl.signal, cache: "no-store" })
    } catch {
      if (!ctl.signal.aborted) es.onerror?.({ status: 0 })
      return
    }
    if (!res.ok || !res.body) { es.onerror?.({ status: res.status }); return }
    es.onopen?.({})
    const reader = res.body.pipeThrough(new TextDecoderStream()).getReader()
    let buf = ""
    let event = "", data: string[] = [], id = ""
    try {
      for (;;) {
        const { value, done } = await reader.read()
        if (done) break
        buf += value
        let nl: number
        while ((nl = buf.search(/\r\n|\n|\r/)) >= 0) {
          const line = buf.slice(0, nl)
          buf = buf.slice(nl + (buf.startsWith("\r\n", nl) ? 2 : 1))
          if (line === "") {
            if (data.length) dispatch(event || "message", data.join("\n"), id)
            event = ""; data = []
            continue
          }
          if (line.startsWith(":")) { dispatch("heartbeat", line.slice(1).trim(), id); continue }
          const c = line.indexOf(":")
          const field = c < 0 ? line : line.slice(0, c)
          const val = c < 0 ? "" : line.slice(c + 1).replace(/^ /, "")
          if (field === "event") event = val
          else if (field === "data") data.push(val)
          else if (field === "id") id = val
        }
      }
      if (!ctl.signal.aborted) es.onerror?.({ status: 0 }) // the server closed the stream
    } catch {
      if (!ctl.signal.aborted) es.onerror?.({ status: 0 })
    }
  })()
  return es
}
