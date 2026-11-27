import { describe, expect, it } from "vitest"
import { foldMessages, initialStore, sessionReducer, type SessionStore } from "./reducer"
import type { Interrupt, Message, Part } from "./types"
import type { StreamMessage, WireEvent } from "./wire"
import { stream } from "./__fixtures__/load"

const live = stream("conversation.live.jsonl")
const fold = (msgs: StreamMessage[], id = "S-ui-0001", from?: SessionStore) => foldMessages(from ?? initialStore(id), msgs)
const isEvent = (m: StreamMessage): m is WireEvent => m.kind === "event"

/** The interrupt cards in thread order. */
function cards(s: SessionStore): Interrupt[] {
  const ids = s.state.messages.flatMap((m) => m.parts.flatMap((p) => (p.type === "interrupt" ? [p.interruptId] : [])))
  return ids.map((id) => s.state.interrupts[id])
}
const parts = (s: SessionStore): Part[] => s.state.messages.flatMap((m) => m.parts)
const rebuildParts = (s: SessionStore) => s.state.messages.filter((m) => m.id.startsWith("run:R-0002")).flatMap((m) => m.parts)
const notices = (s: SessionStore) => parts(s).flatMap((p) => (p.type === "notice" ? [p.text] : []))

describe("sessionReducer over the recorded patent conversation", () => {
  const s = fold(live)

  it("opens with the user's message and titles the session with it", () => {
    const first = s.state.messages[0]
    expect(first.role).toBe("user")
    expect(first.parts).toEqual([{ type: "text", text: "Assess the four invention candidates and prepare the chosen one for counsel." }])
    expect(s.state.title).toBe("Assess the four invention candidates and prepare the chosen one for counsel.")
    expect(s.state.agent).toBe("patent")
    expect(s.state.phase).toBe("ready")
    expect(s.state.stream).toBe("live")
  })

  it("shows the question, the choice and the approval in order, each closed", () => {
    const c = cards(s)
    expect(c.map((i) => [i.id, i.kind, i.state])).toEqual([
      ["I-0001", "input", "resolved"],
      ["I-0002", "choice", "resolved"],
      ["A-0001", "approval", "granted"],
    ])
    expect(c[0].questions?.[0].text).toBe("C2: the note leaves Minimum distance from a vent undecided. What is it?")
    expect(c[0].answer).toBe("0.5 m from the vent edge, so its air flow stays outside the footprint.")
    expect(c[1].questions?.[0].options?.map((o) => o.key)).toEqual(["C1", "C2", "C3", "C4"])
    expect(c[1].questions?.[0].options?.[0].hint).toBe("prioritize counsel review")
    expect(c[1].answer).toContain("C1")
  })

  it("keeps the approval's exact tool, facts, targets and full signature", () => {
    const a = s.state.interrupts["A-0001"]
    expect(a.action?.tool).toBe("send_packet")
    expect(a.action?.facts).toEqual([["packet", "patent.packet/C1@v1"], ["recipient", "counsel-primary"]])
    expect(a.action?.targets).toEqual(["patent.packet/C1@v1"])
    expect(a.action?.signature).toBe("v1:d5ffe1ab8cb85a1f2e56bc962acf1e72243ef7ee65f391386de1cffc28358baf")
    expect(a.action?.signature).toMatch(/^v1:[0-9a-f]{64}$/)
    expect(a.action?.delivered).toBe("sent to counsel_mail")
  })

  it("ends with the outcome notice and the run complete", () => {
    expect(notices(s).at(-1)).toBe("Run complete: completion checks passed.")
    expect(s.state.runs).toHaveLength(1)
    expect(s.state.runs[0]).toMatchObject({ id: "R-0001", agent: "patent", status: "ok", label: "Complete", outcome: "complete" })
    expect(Number(s.state.runs[0].turns)).toBeGreaterThan(20)
  })

  it("shows the follow-up as the user's message in its lane, read by the run", () => {
    const m = s.state.messages.find((x) => x.lane) as Message
    expect(m).toMatchObject({ role: "user", lane: "follow_up", laneState: "consumed" })
    expect(m.parts[0]).toEqual({ type: "text", text: "Keep the packet short." })
  })

  it("groups a run's steps per Task and keeps tool errors with their reason", () => {
    const groups = parts(s).filter((p) => p.type === "step-group")
    expect(groups.length).toBeGreaterThan(20)
    expect(groups.length).toBeLessThan(40) // ~900 events read as a few dozen rows
    expect(groups[0]).toMatchObject({ type: "step-group", label: "Scope" })
    const tools = groups.flatMap((g) => (g.type === "step-group" ? g.steps : []))
    expect(tools.every((t) => t.state !== "running")).toBe(true)
    const web = tools.find((t) => t.name === "web_search")
    expect(web).toMatchObject({ state: "error" })
    expect(web?.refusal).toContain("web_off")
    expect(web?.refusal).toContain("no web search is configured")
    const read = tools.find((t) => t.name === "read_document")
    expect(read?.state).toBe("done")
    expect(read?.durationMs).toBeTypeOf("number")
    expect(read?.summary).toMatch(/^input\//)
  })

  it("counts artifact versions per slot and lists each slot once", () => {
    const packet = s.state.artifacts.find((v) => v.title === "patent.packet/C1")
    expect(packet).toMatchObject({ id: "artifact:patent.packet/C1@v1", ref: "patent.packet/C1@v1", version: "v1", source: "artifact" })
    const slots = s.state.artifacts.filter((v) => v.source === "artifact").map((v) => v.title)
    expect(new Set(slots).size).toBe(slots.length)
    expect(slots).toContain("patent.portfolio")
    // the uploads made before the first message are known files, but not thread items
    expect(s.state.artifacts.filter((v) => v.source === "input")).toHaveLength(22)
    expect(s.state.messages.some((m) => m.parts.some((p) => p.type === "files"))).toBe(false)
  })

  it("is waiting on the person while the first question is open", () => {
    const mid = fold(live.filter((m) => !isEvent(m) || m.seq <= 224))
    expect(mid.state.interrupts["I-0001"].state).toBe("open")
    expect(mid.state.runs[0]).toMatchObject({ status: "wait", label: "Waiting on you" })
  })
})

describe("replay, resume, dedupe and idempotence", () => {
  it("folding the same stream twice changes nothing", () => {
    const once = fold(live)
    const twice = fold(live, "S-ui-0001", once)
    expect(twice.state).toEqual(once.state)
    // event by event the store object is returned untouched
    const e = live.find(isEvent)!
    expect(sessionReducer(once, e)).toBe(once)
  })

  it("skips duplicated events (dedupe by seq)", () => {
    const events = live.filter(isEvent)
    const doubled: StreamMessage[] = events.flatMap((e) => [e, e])
    const a = fold(events)
    const b = fold(doubled)
    expect(b.state.messages).toEqual(a.state.messages)
    expect(b.state.interrupts).toEqual(a.state.interrupts)
  })

  it("resuming after seq 449 reaches the same thread as one uninterrupted stream", () => {
    const head = fold(live.filter((m) => !isEvent(m) || m.seq <= 449))
    expect(head.state.cursor).toBe(449)
    const resumed = fold(stream("conversation.resume-after-449.jsonl"), "S-ui-0001", head)
    const whole = fold(live)
    expect(resumed.state.messages).toEqual(whole.state.messages)
    expect(resumed.state.interrupts).toEqual(whole.state.interrupts)
    expect(resumed.state.runs).toEqual(whole.state.runs)
  })

  it("skips unknown event types but advances the cursor", () => {
    const base = fold(live)
    const head = base.state.cursor!
    const next = sessionReducer(base, { kind: "event", seq: head + 1, type: "something.new", payload: { anything: 1 } })
    expect(next.state.cursor).toBe(head + 1)
    expect(next.state.messages).toBe(base.state.messages)
  })
})

describe("routing, steering and stop", () => {
  const s = fold(stream("route-and-stop.live.jsonl"), "S-ui-0002")

  it("says where the classifier routed, shows the steer, voids the question and ends stopped", () => {
    expect(s.state.messages[0]).toMatchObject({ role: "user" })
    expect(notices(s)[0]).toMatch(/^Routed to research \(95% match\)$/)
    expect(s.state.interrupts["I-0004"]).toMatchObject({ kind: "input", state: "voided", detail: "Run stopped." })
    const steer = s.state.messages.find((m) => m.lane === "steer")
    expect(steer?.parts[0]).toEqual({ type: "text", text: "Only EU vendors." })
    expect(notices(s).at(-1)).toBe("Run stopped: stop requested.")
    expect(s.state.runs[0]).toMatchObject({ id: "R-0004", agent: "research", status: "idle", label: "Stopped" })
    // the 25 owner-wide uploads before this session's first message stay out of its thread
    expect(s.state.messages.filter((m) => m.role === "system")).toHaveLength(0)
  })
})

describe("re-upload and rebuild", () => {
  const before = fold(live)
  const after = fold(stream("reupload.tail.jsonl"), "S-ui-0001", before)

  it("shows the changed files with their version change and the source change", () => {
    const sys = after.state.messages.at(-1)!
    expect(sys.role).toBe("system")
    const files = sys.parts.find((p) => p.type === "files")
    expect(files?.type === "files" && files.files.map((f) => f.title)).toContain("disclosures/history.csv")
    const history = files?.type === "files" ? files.files.find((f) => f.title === "disclosures/history.csv") : undefined
    expect(history?.versions).toEqual(["v1", "v2"])
    expect(notices(after).some((n) => n.startsWith("Inputs changed: disclosures/consultant-email.eml.txt, disclosures/history.csv"))).toBe(true)
    expect(after.state.artifacts.find((v) => v.title === "disclosures/history.csv")?.version).toBe("v2")
  })

  // The recorded server did not yet send rebuild runs to the session stream (the reads spec
  // fixes that). These events follow the runtime's shapes for a rebuild run, a review and a citation.
  it("groups a rebuild run's work under its notice and raises the review with its card hash", () => {
    let seq = after.state.cursor!
    const ev = (type: string, payload: Record<string, unknown>, extra: Partial<WireEvent> = {}): WireEvent =>
      ({ kind: "event", seq: ++seq, type, run: "R-0002", session: null, at: "2026-11-27T18:11:38Z", payload, ...extra })
    const s = fold([
      ev("run.started", { agent: "patent", cause: "rebuild", for_runs: ["R-0001"] }),
      ev("task.entered", { task: "packet" }, { instance: "packet[C1]" }),
      ev("step.completed", { kind: "model", value: { result: { text: null, tool_calls: [{ name: "save_artifact", arguments: { kind: "patent.packet" } }] } } }, { instance: "packet[C1]", step: "R-0002/packet[C1]/t01" }),
      ev("step.started", { kind: "tool", tool: "save_artifact" }, { instance: "packet[C1]", step: "R-0002/packet[C1]/t01/c01" }),
      ev("artifact.saved", { kind: "patent.packet", key: "C1" }, { instance: "packet[C1]", step: "R-0002/packet[C1]/t01/c01" }),
      ev("step.completed", { kind: "tool", tool: "save_artifact", outcome: "ok", duration_ms: 4 }, { instance: "packet[C1]", step: "R-0002/packet[C1]/t01/c01" }),
      ev("citation.bound", { carrier: "patent.packet/C1@v2", pointer: "/dates/0", target: "EX-0018", quote: "2025-11-21", form: "span" }),
      ev("interrupt.raised", {
        interrupt: "I-0003", kind: "review", old: "patent.packet/C1@v1", revision: "patent.packet/C1@v2", card_hash: "sha256:abc",
        hunks: [{ path: "/dates/0", old: "2026-01-05", new: "2025-11-21" }], via: [],
      }, { instance: "packet[C1]" }),
      ev("step.completed", { kind: "tool", tool: "fill_form", outcome: "refused", code: "stale_target", value: { view: { status: "refused", text: "{\"message\":\"the packet moved\"}" } } }, { instance: "packet[C1]", step: "R-0002/packet[C1]/t02/c01" }),
      ev("run.ended", { outcome: "complete", reason: "completion_checks_passed" }),
    ], "S-ui-0001", after)

    const rebuild = s.state.messages.find((m) => m.id.startsWith("run:R-0002"))!
    expect(rebuild.parts[0]).toEqual({ type: "notice", tone: "idle", text: "Rebuilding after an input change: this run reworks R-0001" })
    const group = rebuild.parts.find((p) => p.type === "step-group")
    expect(group).toMatchObject({ label: "Packet C1" })
    expect(group?.type === "step-group" && group.saved?.[0].ref).toBe("patent.packet/C1@v2")
    expect(s.state.runs.find((r) => r.id === "R-0002")).toMatchObject({ forRuns: ["R-0001"], status: "ok" })
    const review = s.state.interrupts["I-0003"]
    expect(review.review).toMatchObject({ artifact: "patent.packet/C1", from: "v1", to: "v2", changes: 1, cardHash: "sha256:abc" })
    expect(s.state.artifacts.find((v) => v.ref === "patent.packet/C1@v2")?.citations).toEqual([
      { pointer: "/dates/0", target: "EX-0018", quote: "2025-11-21", form: "span" },
    ])
    const refused = rebuildParts(s).flatMap((p) => (p.type === "step-group" ? p.steps : [])).find((t) => t.name === "fill_form")
    expect(refused).toMatchObject({ state: "refused", refusal: "stale_target: the packet moved" })
  })
})

describe("optimistic messages", () => {
  it("reconciles the optimistic message when the stream confirms it, keyed by command ID", () => {
    let s = sessionReducer(initialStore("S-new"), { kind: "local", action: { type: "optimistic-message", commandId: "c-1", text: "Hello", at: "2026-11-27T10:00:00Z" } })
    expect(s.state.messages).toHaveLength(1)
    expect(s.state.messages[0]).toMatchObject({ id: "opt:c-1", pending: true })
    s = fold([
      { kind: "event", seq: 1, type: "command.accepted", session: "S-new", payload: { command_id: "c-1", name: "send_message" } },
      { kind: "event", seq: 2, type: "user.message", session: "S-new", at: "2026-11-27T10:00:01Z", payload: { message: "M-1", text: "Hello" } },
    ], "S-new", s)
    expect(s.state.messages).toHaveLength(1)
    expect(s.state.messages[0]).toMatchObject({ id: "opt:c-1", pending: false, at: "2026-11-27T10:00:01Z" })
  })

  it("drops the optimistic message when the command is refused", () => {
    let s = sessionReducer(initialStore("S-new"), { kind: "local", action: { type: "optimistic-message", commandId: "c-2", text: "Hi", at: "" } })
    s = sessionReducer(s, { kind: "local", action: { type: "command-refused", commandId: "c-2" } })
    expect(s.state.messages).toHaveLength(0)
  })

  it("reconciles a steer by the command ID on input.queued", () => {
    let s = sessionReducer(initialStore("S-x"), { kind: "local", action: { type: "optimistic-message", commandId: "c-steer", text: "Only EU", at: "", lane: "steer" } })
    s = sessionReducer(s, { kind: "event", seq: 5, type: "input.queued", run: "R-1", payload: { command_id: "c-steer", input_id: "IN-1", lane: "steer", text: "Only EU" } })
    expect(s.state.messages).toHaveLength(1)
    expect(s.state.messages[0]).toMatchObject({ id: "opt:c-steer", lane: "steer", laneState: "queued", pending: false })
  })
})
