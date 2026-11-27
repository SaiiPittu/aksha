import { describe, expect, it, vi } from "vitest"
import { render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { TooltipProvider } from "@/components/ui/tooltip"
import { ApiError } from "@/api"
import { InterruptCard } from "./Interrupts"
import { Composer } from "./Composer"
import { foldMessages, initialStore } from "@/features/session/reducer"
import { sessionCommands } from "@/features/session/commands"
import type { UploadedFile } from "@/features/session/types"
import { stream } from "@/features/session/__fixtures__/load"
import { mockFetch, apiError, json } from "@/test/utils"

const SIG = "v1:d5ffe1ab8cb85a1f2e56bc962acf1e72243ef7ee65f391386de1cffc28358baf"
const wrap = (ui: React.ReactNode) => render(<TooltipProvider>{ui}</TooltipProvider>)

/** The session at the moment the recorded run asked for the send approval. */
const atApproval = () => foldMessages(initialStore("S-ui-0001"), stream("conversation.live.jsonl").filter((m) => m.kind !== "event" || m.seq <= 870))

describe("approval card", () => {
  it("shows the exact tool, facts, targets and the full signature", () => {
    const a = atApproval().state.interrupts["A-0001"]
    expect(a.state).toBe("open")
    wrap(<InterruptCard i={a} onOpenView={() => {}} onAct={vi.fn()} />)
    expect(screen.getByText("send_packet")).toBeInTheDocument()
    expect(screen.getByText("counsel-primary")).toBeInTheDocument()
    expect(screen.getAllByText("patent.packet/C1@v1")).toHaveLength(2) // the packet fact and the target
    expect(screen.getByLabelText("Signature")).toHaveTextContent(SIG)
  })

  it("never focuses Approve by default, and sends the full signature", async () => {
    const a = atApproval().state.interrupts["A-0001"]
    const onAct = vi.fn(() => Promise.resolve())
    wrap(<InterruptCard i={a} onOpenView={() => {}} onAct={onAct} />)
    const approve = screen.getByRole("button", { name: /approve and send/i })
    expect(approve).not.toHaveFocus()
    expect(document.activeElement === document.body || document.activeElement === null).toBe(true)
    await userEvent.click(approve)
    expect(onAct).toHaveBeenCalledWith({ kind: "approve", signature: SIG })
  })

  it("shows a refused approval's code inline and stays pending", async () => {
    const a = atApproval().state.interrupts["A-0001"]
    const onAct = vi.fn(() => Promise.reject(new ApiError(409, "signature_mismatch", "Approve refused")))
    wrap(<InterruptCard i={a} onOpenView={() => {}} onAct={onAct} />)
    await userEvent.click(screen.getByRole("button", { name: /approve and send/i }))
    expect(await screen.findByText("signature_mismatch")).toBeInTheDocument()
    expect(screen.getByRole("button", { name: /approve and send/i })).toBeEnabled()
  })

  it("rejects with a note", async () => {
    const a = atApproval().state.interrupts["A-0001"]
    const onAct = vi.fn(() => Promise.resolve())
    wrap(<InterruptCard i={a} onOpenView={() => {}} onAct={onAct} />)
    await userEvent.click(screen.getByRole("button", { name: "Reject" }))
    await userEvent.type(screen.getByLabelText("Rejection note"), "Wrong recipient")
    await userEvent.click(screen.getByRole("button", { name: "Reject" }))
    expect(onAct).toHaveBeenCalledWith({ kind: "reject", note: "Wrong recipient" })
  })

  it("approve posts the full signature as shown_signature", async () => {
    const { calls } = mockFetch({ "POST /api/v1/approvals/A-0001/approve": json({ receipt: { command_id: "x", seq: 876, status: "accepted", code: null, replayed: false } }) })
    const store = atApproval()
    const cmds = sessionCommands({ getState: () => store.state, dispatch: () => {} })
    await cmds.act("A-0001", { kind: "approve", signature: store.state.interrupts["A-0001"].action!.signature })
    expect(calls[0].body).toMatchObject({ shown_signature: SIG })
    expect(typeof (calls[0].body as { command_id: string }).command_id).toBe("string")
  })
})

describe("question card", () => {
  it("answers a choice by its option key", async () => {
    const s = foldMessages(initialStore("S-ui-0001"), stream("conversation.live.jsonl").filter((m) => m.kind !== "event" || m.seq <= 740))
    const i = s.state.interrupts["I-0002"]
    const onAct = vi.fn(() => Promise.resolve())
    wrap(<InterruptCard i={i} onOpenView={() => {}} onAct={onAct} />)
    await userEvent.click(screen.getByRole("radio", { name: /C1/ }))
    await userEvent.click(screen.getByRole("button", { name: /submit/i }))
    expect(onAct).toHaveBeenCalledWith({ kind: "answer", answers: { q1: "C1" }, text: undefined })
  })
})

describe("composer lane prompt", () => {
  it("asks for a lane when the server says lane_required, keeps the text, then sends in the lane", async () => {
    const onSend = vi.fn()
      .mockRejectedValueOnce(new ApiError(409, "lane_required", "SendMessage refused"))
      .mockResolvedValueOnce(undefined)
    wrap(<Composer onSend={onSend} runOpen={false} />)
    const box = screen.getByLabelText("Message")
    await userEvent.type(box, "and also C2{Enter}")
    expect(onSend).toHaveBeenNthCalledWith(1, { text: "and also C2", lane: undefined, files: undefined })
    expect(await screen.findByText("Pick how this message applies")).toBeInTheDocument()
    expect(box).toHaveValue("and also C2")
    expect(screen.getByRole("button", { name: /send/i })).toBeDisabled()
    await userEvent.click(screen.getByRole("radio", { name: "Follow-up" }))
    await userEvent.click(screen.getByRole("button", { name: /send/i }))
    expect(onSend).toHaveBeenNthCalledWith(2, { text: "and also C2", lane: "follow_up", files: undefined })
    await waitFor(() => expect(box).toHaveValue(""))
  })

  it("shows other refusals inline and keeps the draft", async () => {
    const onSend = vi.fn().mockRejectedValue(new ApiError(409, "run_ended", "Steer refused"))
    wrap(<Composer onSend={onSend} runOpen />)
    await userEvent.type(screen.getByLabelText("Message"), "stop there")
    await userEvent.click(screen.getByRole("radio", { name: "Steer this run" }))
    await userEvent.click(screen.getByRole("button", { name: /send/i }))
    expect(await screen.findByRole("alert")).toHaveTextContent("run_ended")
    expect(screen.getByLabelText("Message")).toHaveValue("stop there")
  })

  it("uploads attached files with progress, then shows a re-upload as changed", async () => {
    let resolve!: (f: UploadedFile[]) => void
    const onUpload = vi.fn((_files: File[], onProgress?: (p: { path: string; loaded: number; total: number }[]) => void) => {
      onProgress?.([{ path: "history.csv", loaded: 5, total: 10 }])
      return new Promise<UploadedFile[]>((r) => { resolve = r })
    })
    const { container } = wrap(<Composer onSend={vi.fn()} onUpload={onUpload} runOpen={false} />)
    const input = container.querySelector("input[type=file]") as HTMLInputElement
    await userEvent.upload(input, new File(["a,b\n1,2"], "history.csv", { type: "text/csv" }))
    expect(await screen.findByText("50%")).toBeInTheDocument()
    resolve([{ path: "history.csv", change: "changed", from: 1, to: 2, view: { id: "input:history.csv@v2", kind: "table", title: "history.csv", source: "input" } }])
    expect(await screen.findByText("changed (v1 → v2)")).toBeInTheDocument()
  })
})

describe("session commands", () => {
  it("learns the open run from a lane_required refusal, then steers it", async () => {
    const { calls } = mockFetch({
      "POST /api/v1/sessions/S-ui-0001/messages": apiError(409, "lane_required", "SendMessage refused", { status: "refused", code: "lane_required" }),
      "GET /api/v1/sessions/S-ui-0001": json({ session: "S-ui-0001", run: { run: "R-0001", state: "waiting" }, waiting_on: [], seq: 300 }),
      "POST /api/v1/runs/R-0001/steer": json({ receipt: { command_id: "x", seq: 301, status: "accepted", code: null, replayed: false } }),
    })
    const state = initialStore("S-ui-0001").state
    const dispatch = vi.fn()
    const cmds = sessionCommands({ getState: () => state, dispatch })
    await expect(cmds.send({ text: "Keep the packet short." })).rejects.toMatchObject({ code: "lane_required" })
    expect(dispatch).toHaveBeenCalledWith(expect.objectContaining({ type: "command-refused" }))
    await cmds.send({ text: "Keep the packet short.", lane: "follow_up" })
    expect(calls.at(-1)).toMatchObject({ method: "POST", path: "/api/v1/runs/R-0001/steer", body: { text: "Keep the packet short.", lane: "follow_up" } })
  })

  it("a new chat names its session S-<hex> and opens it after the receipt", async () => {
    const calls: { path: string; body: unknown }[] = []
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      calls.push({ path: new URL(String(input), "http://x").pathname, body: JSON.parse(String(init?.body)) })
      return new Response(JSON.stringify({ receipt: { command_id: "c", seq: 1, status: "accepted", code: null, replayed: false } }), { status: 200 })
    }))
    const onNewSession = vi.fn()
    const cmds = sessionCommands({ getState: () => initialStore(null).state, dispatch: vi.fn(), onNewSession })
    await cmds.send({ text: "Hello", agent: "research" })
    const path = calls.at(-1)!.path
    expect(path).toMatch(/^\/api\/v1\/sessions\/S-[0-9a-f]{12}\/messages$/)
    expect(calls.at(-1)!.body).toMatchObject({ text: "Hello", agent: "research" })
    const id = path.split("/")[4]
    expect(onNewSession).toHaveBeenCalledWith(id, expect.objectContaining({ type: "optimistic-message", text: "Hello" }))
  })
})
