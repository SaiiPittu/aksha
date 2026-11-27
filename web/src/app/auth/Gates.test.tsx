import { screen } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"
import { routes } from "@/router"
import { apiError, json, mockFetch, renderRoutes } from "@/test/utils"

afterEach(() => vi.unstubAllGlobals())

const me = { user: { id: "u-1", email: "ana@example.com", name: "Ana Lima" }, org: { id: "o-1", name: "Example Org" },
  role: "owner", csrf: "c", tenant: "u-1", is_guest: false, guest_expires_at: null }

describe("the route guard in the app", () => {
  it("local mode opens the app with the claim banner", async () => {
    mockFetch({
      "GET /api/v1/auth/status": json({ mode: "local", guests: false, signup_open: false }),
      "GET /api/v1/auth/me": json({ ...me, user: null, org: null, tenant: "local", csrf: null }),
    })
    renderRoutes(routes, "/")
    expect(await screen.findByRole("button", { name: "Claim this workspace" })).toBeInTheDocument()
    expect(screen.getByText(/Local mode/)).toBeInTheDocument()
  })

  it("signed out lands on sign in with the path kept", async () => {
    mockFetch({
      "GET /api/v1/auth/status": json({ mode: "accounts", guests: false, signup_open: false }),
      "GET /api/v1/auth/me": apiError(401, "unauthenticated", "sign in first"),
    })
    const { router } = renderRoutes(routes, "/settings/tokens")
    expect(await screen.findByRole("form", { name: "Sign in" })).toBeInTheDocument()
    expect(router.state.location.pathname).toBe("/sign-in")
    expect(router.state.location.search).toBe("?next=%2Fsettings%2Ftokens")
  })

  it("signed in shows the page, with the viewer in the rail", async () => {
    mockFetch({
      "GET /api/v1/auth/status": json({ mode: "accounts", guests: false, signup_open: false }),
      "GET /api/v1/auth/me": json(me),
      "GET /api/v1/tokens": json({ tokens: [{ id: "t-1", name: "work laptop", created_at: null, last_used_at: null }] }),
    })
    renderRoutes(routes, "/settings/tokens")
    expect(await screen.findByText("work laptop")).toBeInTheDocument()
    expect(screen.getByText("Ana Lima")).toBeInTheDocument()
  })
})
