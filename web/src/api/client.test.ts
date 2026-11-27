import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { ApiError, Command, request, setCsrfToken } from "./client"
import { auth, commands } from "./endpoints"
import { apiError, json, mockFetch } from "@/test/utils"

const me = { user: { id: "u-1", email: "ana@example.com", name: "Ana" }, org: { id: "o-1", name: "Example Org" },
  role: "owner", csrf: "csrf-abc", tenant: "u-1", is_guest: false, guest_expires_at: null }
const receipt = (id: string) => ({ receipt: { command_id: id, seq: 7, status: "accepted", code: null, replayed: false } })

beforeEach(() => setCsrfToken(null))
afterEach(() => vi.unstubAllGlobals())

describe("CSRF", () => {
  it("sends the token from /auth/me on unsafe methods, and never on GET", async () => {
    const { calls } = mockFetch({
      "GET /api/v1/auth/me": json(me),
      "POST /api/v1/org/invites": json({ invites: [] }),
      "DELETE /api/v1/tokens/t-1": { status: 204 },
      "GET /api/v1/org/members": json([]),
    })
    await auth.me()
    await request("POST", "/org/invites", { body: { emails: [], role: "member" } })
    await request("DELETE", "/tokens/t-1")
    await request("GET", "/org/members")
    expect(calls[0].headers["X-CSRF-Token"]).toBeUndefined()
    expect(calls[1].headers["X-CSRF-Token"]).toBe("csrf-abc")
    expect(calls[2].headers["X-CSRF-Token"]).toBe("csrf-abc")
    expect(calls[3].headers["X-CSRF-Token"]).toBeUndefined()
  })

  it("sends cookies with every request", async () => {
    const { fn } = mockFetch({ "GET /api/v1/auth/status": json({ mode: "accounts", guests: false, signup_open: false }) })
    await auth.status()
    expect(fn.mock.calls[0][1]?.credentials).toBe("include")
  })

  it("forgets the token on sign out", async () => {
    const { calls } = mockFetch({ "GET /api/v1/auth/me": json(me), "POST /api/v1/auth/logout": { status: 204 }, "POST /api/v1/auth/password": { status: 204 } })
    await auth.me()
    await auth.logout()
    await request("POST", "/auth/password", { body: {} })
    expect(calls[1].headers["X-CSRF-Token"]).toBe("csrf-abc")
    expect(calls[2].headers["X-CSRF-Token"]).toBeUndefined()
  })
})

describe("errors", () => {
  it("maps the server's error shape to a typed ApiError", async () => {
    mockFetch({ "POST /api/v1/auth/login": apiError(429, "throttled", "Too many attempts. Try again in 15 minutes.", { retry_after: 900 }) })
    const e = await auth.login({ email: "a@example.com", password: "x", remember: true }).catch((x) => x)
    expect(e).toBeInstanceOf(ApiError)
    expect(e).toMatchObject({ status: 429, code: "throttled", message: "Too many attempts. Try again in 15 minutes.", details: { retry_after: 900 } })
  })

  it("keeps the status when the body is not the error shape", async () => {
    mockFetch({ "GET /api/v1/org/members": { status: 502, body: "<html>bad gateway</html>" } })
    const e = await request("GET", "/org/members").catch((x) => x)
    expect(e).toMatchObject({ status: 502, code: "http_502" })
    expect((e as ApiError).retryable).toBe(true)
  })

  it("turns a failed fetch into code network", async () => {
    mockFetch({ "GET /api/v1/auth/status": new TypeError("Failed to fetch") })
    const e = await auth.status().catch((x) => x)
    expect(e).toMatchObject({ status: 0, code: "network" })
  })

  it("rejects a response that does not match the schema with bad_response", async () => {
    mockFetch({ "GET /api/v1/auth/status": json({ mode: "something-else" }) })
    const e = await auth.status().catch((x) => x)
    expect(e).toMatchObject({ code: "bad_response" })
  })
})

describe("command IDs", () => {
  const idOf = (c: { body: unknown }) => (c.body as { command_id: string }).command_id

  it("one command per click: automatic retries reuse its ID, a new click gets a new one", async () => {
    const { calls } = mockFetch({
      "POST /api/v1/runs/R-1/stop": (call, n) => (n === 1 ? apiError(503, "unavailable", "busy") : json(receipt(idOf(call)))),
    })
    const click = commands.stop("R-1")
    expect(click.id).toMatch(/^c-[0-9a-f-]{36}$/)
    const r = await click.send()
    expect(r).toMatchObject({ status: "accepted", command_id: click.id })
    expect(calls.map(idOf)).toEqual([click.id, click.id])

    const again = commands.stop("R-1")
    expect(again.id).not.toBe(click.id)
  })

  it("a manual retry of the same click sends the same ID", async () => {
    const { calls } = mockFetch({
      "POST /api/v1/interrupts/I-1/answer": (call, n) => (n === 1 ? new TypeError("offline") : json(receipt(idOf(call)))),
    })
    const click = new Command("POST", "/interrupts/I-1/answer", { text: "yes" }, undefined, { retries: 0 })
    await expect(click.send()).rejects.toMatchObject({ code: "network" })
    await click.send()
    expect(calls.map(idOf)).toEqual([click.id, click.id])
    expect(calls[1].body).toMatchObject({ text: "yes" })
  })

  it("does not retry a refusal", async () => {
    const { calls } = mockFetch({ "POST /api/v1/approvals/A-1/approve": apiError(409, "signature_mismatch", "Approve refused") })
    await expect(commands.approve("A-1", "sig-1").send()).rejects.toMatchObject({ status: 409, code: "signature_mismatch" })
    expect(calls).toHaveLength(1)
    expect(calls[0].body).toMatchObject({ shown_signature: "sig-1" })
  })
})
