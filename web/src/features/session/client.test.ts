import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { SessionClient, type EventSourceLike, type SseEvent } from "./client"
import { foldMessages, initialStore } from "./reducer"
import type { StreamMessage } from "./wire"
import { blocks, stream } from "./__fixtures__/load"

/** A fake connection: the test plays the server. */
class FakeSource implements EventSourceLike {
  static all: FakeSource[] = []
  onopen: ((ev: unknown) => void) | null = null
  onerror: ((ev: unknown) => void) | null = null
  closed = false
  private ls = new Map<string, ((ev: SseEvent) => void)[]>()
  readonly url: string
  constructor(url: string) { this.url = url; FakeSource.all.push(this) }
  addEventListener(type: string, fn: (ev: SseEvent) => void) { this.ls.set(type, [...(this.ls.get(type) ?? []), fn]) }
  close() { this.closed = true }
  open() { this.onopen?.({}) }
  fail(status = 0) { this.onerror?.({ status }) }
  /** Like the fetch reader: a type without a listener reaches "message". */
  send(type: string, data: unknown, id = "") {
    const fns = this.ls.get(type) ?? this.ls.get("message") ?? []
    for (const f of fns) f({ type, data: JSON.stringify(data), lastEventId: id })
  }
  heartbeat() { for (const f of this.ls.get("heartbeat") ?? []) f({ type: "heartbeat", data: "heartbeat", lastEventId: "" }) }
  event(seq: number, type = "stop.decided", extra: object = {}) {
    this.send(type, { kind: "event", seq, type, run: "R-1", payload: {}, ...extra }, String(seq))
  }
}

const last = () => FakeSource.all.at(-1)!

function setup(opts: Partial<ConstructorParameters<typeof SessionClient>[0]> = {}) {
  const got: StreamMessage[] = []
  const c = new SessionClient({
    sessionId: "S-ui-0001", connect: (u) => new FakeSource(u), onMessages: (b) => got.push(...b),
    backoff: (n) => 1000 * n, ...opts,
  })
  c.start()
  return { c, got, flush: () => c.flush() }
}

const kinds = (got: StreamMessage[]) => got.map((m) => (m.kind === "event" ? `e${m.seq}` : m.kind === "stream" ? `stream:${m.state}` : m.kind))

beforeEach(() => { FakeSource.all = []; vi.useFakeTimers() })
afterEach(() => { vi.useRealTimers() })

describe("SessionClient", () => {
  it("opens one stream for the session with credentials-ready URL and no cursor at first", () => {
    setup()
    expect(FakeSource.all).toHaveLength(1)
    expect(last().url).toBe("/api/v1/sessions/S-ui-0001/events")
  })

  it("replays, then goes live on synchronized, tracking the cursor from the SSE id", () => {
    const { c, got, flush } = setup()
    last().open()
    last().event(1); last().event(2)
    last().send("synchronized", { kind: "synchronized", seq: 2 })
    flush()
    expect(kinds(got)).toEqual(["stream:replaying", "e1", "e2", "synchronized"])
    expect(c.position).toBe(2)
    expect(c.streamState).toBe("live")
  })

  it("reconnects with backoff after an error and resumes from the cursor", () => {
    const { c, got, flush } = setup()
    last().open(); last().event(7); last().send("synchronized", { kind: "synchronized", seq: 7 })
    last().fail()
    flush()
    expect(c.streamState).toBe("reconnecting")
    expect(last().closed).toBe(true)
    vi.advanceTimersByTime(999)
    expect(FakeSource.all).toHaveLength(1)
    vi.advanceTimersByTime(1)
    expect(FakeSource.all).toHaveLength(2)
    expect(last().url).toBe("/api/v1/sessions/S-ui-0001/events?after=7")
    last().open(); last().event(8); flush()
    expect(kinds(got)).toEqual(["stream:replaying", "e7", "synchronized", "stream:reconnecting", "stream:replaying", "e8"])
  })

  it("backs off longer on each failed attempt and resets after synchronized", () => {
    setup()
    last().fail(); vi.advanceTimersByTime(1000); expect(FakeSource.all).toHaveLength(2)
    last().fail(); vi.advanceTimersByTime(1999); expect(FakeSource.all).toHaveLength(2)
    vi.advanceTimersByTime(1); expect(FakeSource.all).toHaveLength(3)
    last().open(); last().send("synchronized", { kind: "synchronized", seq: 0 })
    last().fail(); vi.advanceTimersByTime(1000); expect(FakeSource.all).toHaveLength(4)
  })

  it("counts a stream silent for over 30 s as dropped; heartbeats keep it alive", () => {
    const { c } = setup()
    last().open(); last().event(3); last().send("synchronized", { kind: "synchronized", seq: 3 })
    vi.advanceTimersByTime(20_000); last().heartbeat()
    vi.advanceTimersByTime(20_000); last().heartbeat()
    expect(c.streamState).toBe("live")
    vi.advanceTimersByTime(30_000)
    expect(c.streamState).toBe("reconnecting")
    vi.advanceTimersByTime(1000)
    expect(last().url).toBe("/api/v1/sessions/S-ui-0001/events?after=3")
  })

  it("skips events at or below the cursor and unknown types, but advances past unknown ones", () => {
    const { c, got, flush } = setup({ cursor: 10 })
    expect(last().url).toBe("/api/v1/sessions/S-ui-0001/events?after=10")
    last().open(); last().event(9); last().event(10); last().event(11)
    last().send("brand.new", { kind: "event", seq: 12, type: "brand.new", payload: {} }, "12")
    last().send("brand.new", { kind: "mystery" }, "13")
    flush()
    expect(kinds(got)).toEqual(["stream:replaying", "e11", "e12"])
    expect(c.position).toBe(13)
  })

  it("on resync refetches views, then returns to live", async () => {
    const onResync = vi.fn(() => Promise.resolve())
    const { c, got, flush } = setup({ onResync })
    last().open(); last().send("resync", { kind: "resync", seq: 40 }); flush()
    expect(onResync).toHaveBeenCalledWith(40)
    expect(c.streamState).toBe("refetching")
    await vi.waitFor(() => expect(c.streamState).toBe("live"))
    flush()
    expect(kinds(got)).toEqual(["stream:replaying", "resync", "stream:live"])
  })

  it("stops for good on 404 and reports not found", () => {
    const { got, flush } = setup()
    last().fail(404); flush()
    vi.advanceTimersByTime(60_000)
    expect(FakeSource.all).toHaveLength(1)
    expect(got.at(-1)).toMatchObject({ kind: "local", action: { type: "failed", code: "not_found" } })
  })

  it("close() stops the stream and any reconnect", () => {
    const { c } = setup()
    last().fail(); c.close()
    vi.advanceTimersByTime(60_000)
    expect(FakeSource.all).toHaveLength(1)
  })

  it("delivers a recorded stream so the fold equals folding the recording directly", () => {
    const { got, flush } = setup()
    last().open()
    for (const b of blocks("route-and-stop.live.jsonl")) {
      if (b.comment) last().heartbeat()
      else last().send(b.sse_event ?? "message", b.data, b.sse_id ?? "")
    }
    flush()
    const viaClient = foldMessages(initialStore("S-ui-0002"), got)
    const direct = foldMessages(initialStore("S-ui-0002"), stream("route-and-stop.live.jsonl"))
    expect(viaClient.state.messages).toEqual(direct.state.messages)
    expect(viaClient.state.stream).toBe("live")
  })
})
