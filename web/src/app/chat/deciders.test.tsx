import { describe, expect, it, vi } from "vitest"
import { screen } from "@testing-library/react"
import { render } from "@testing-library/react"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { TooltipProvider } from "@/components/ui/tooltip"
import type { Me, Member } from "@/api"
import type { Interrupt } from "@/features/session/types"
import { mockFetch, json } from "@/test/utils"
import { InterruptCard } from "./Interrupts"
import { DeciderNames, deciderName } from "./deciders"

// The server records `by` as `user:<id>` or `local` (ADR-P19 B19); a card never shows it raw.

const owner: Me = { user: { id: "u-own" }, role: "owner", tenant: "u-own", is_guest: false }
const member: Me = { user: { id: "u-mem" }, role: "member", tenant: "u-mem", is_guest: false }
const local: Me = { user: null, role: "owner", tenant: "local", is_guest: false }
const members = [{ id: "u-ada", user_id: "u-ada", email: "ada@example.org", name: "Ada Admin", role: "admin", active: true }] as Member[]

describe("deciderName", () => {
  it("is 'you' for the viewer's own user or local mode", () => {
    expect(deciderName("user:u-own", owner, members)).toBe("you")
    expect(deciderName("local", local)).toBe("you")
  })
  it("is the member's name when the viewer may read the members, else 'another member'", () => {
    expect(deciderName("user:u-ada", owner, members)).toBe("Ada Admin")
    expect(deciderName("user:u-ada", member, undefined)).toBe("another member")
    expect(deciderName("user:u-gone", owner, members)).toBe("another member")
  })
  it("keeps a role name and nothing for no by", () => {
    expect(deciderName("data-steward", owner, members)).toBe("data-steward")
    expect(deciderName(undefined, owner, members)).toBeUndefined()
  })
})

const approval = (decidedBy: string, state: Interrupt["state"] = "granted"): Interrupt => ({
  id: "A-0001", kind: "approval", state, title: "Send the packet", decidedBy,
  action: { tool: "send_packet", facts: [], targets: [], signature: "v1:abc" },
} as unknown as Interrupt)

function show(i: Interrupt, me: Me) {
  const { calls } = mockFetch({ "GET /api/v1/auth/me": json({ ...me, csrf: "t" }), "GET /api/v1/org/members": json({ members }) })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={qc}><TooltipProvider><DeciderNames>
    <InterruptCard i={i} onOpenView={vi.fn()} />
  </DeciderNames></TooltipProvider></QueryClientProvider>)
  return calls
}

describe("a decided approval card", () => {
  it("says 'you' for the viewer's own decision, never the raw id", async () => {
    show(approval("user:u-own"), owner)
    expect(await screen.findByText(/Approved by you and executed/)).toBeInTheDocument()
    expect(screen.queryByText(/user:u-own/)).toBeNull()
  })
  it("names another member for an owner", async () => {
    show(approval("user:u-ada", "rejected"), owner)
    expect(await screen.findByText(/Rejected by Ada Admin\./)).toBeInTheDocument()
  })
  it("says 'another member' to a member, who may not list the members", async () => {
    const calls = show(approval("user:u-ada"), member)
    expect(await screen.findByText(/Approved by another member and executed/)).toBeInTheDocument()
    expect(calls.some((c) => c.path === "/api/v1/org/members")).toBe(false)
  })
})
