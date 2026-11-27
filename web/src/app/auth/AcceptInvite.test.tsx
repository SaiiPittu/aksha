import { screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { afterEach, describe, expect, it, vi } from "vitest"
import { apiError, json, mockFetch, renderRoutes } from "@/test/utils"
import { AcceptInvite } from "./AcceptInvite"

afterEach(() => vi.unstubAllGlobals())

const routes = [{ path: "/invite/:token", element: <AcceptInvite /> }, { path: "/", element: <p>home</p> }, { path: "/sign-in", element: <p>sign in page</p> }]
const preview = (state: string) => json({
  org: { name: "Example Org" }, inviter: { name: "Ana Lima" }, role: "member", email: "ben@example.com", state,
  expires_at: new Date(Date.now() + 3 * 86_400_000).toISOString(),
})

describe("AcceptInvite", () => {
  it("shows who invited you, to what, as which role, with the email fixed", async () => {
    mockFetch({ "GET /api/v1/auth/invites/tok-1": preview("valid") })
    renderRoutes(routes, "/invite/tok-1")
    expect(await screen.findByText("Ana Lima")).toBeInTheDocument()
    expect(screen.getByText("Example Org")).toBeInTheDocument()
    expect(screen.getByLabelText("Email")).toHaveValue("ben@example.com")
    expect(screen.getByLabelText("Email")).toHaveAttribute("readonly")
    expect(screen.getByText(/expires in [23] days/)).toBeInTheDocument()
  })

  it("checks name and password before sending", async () => {
    const { calls } = mockFetch({ "GET /api/v1/auth/invites/tok-1": preview("valid") })
    renderRoutes(routes, "/invite/tok-1")
    await userEvent.type(await screen.findByLabelText("Password"), "short")
    await userEvent.click(screen.getByRole("button", { name: /Join Example Org/ }))
    expect(screen.getByText("Enter your name.")).toBeInTheDocument()
    expect(screen.getByText("Use at least 8 characters.")).toBeInTheDocument()
    expect(calls.filter((c) => c.method === "POST")).toHaveLength(0)
  })

  it("shows the server's refusal and keeps the form", async () => {
    mockFetch({
      "GET /api/v1/auth/invites/tok-1": preview("valid"),
      "POST /api/v1/auth/invites/tok-1/accept": apiError(409, "invite_used", "This invite was already used."),
    })
    renderRoutes(routes, "/invite/tok-1")
    await userEvent.type(await screen.findByLabelText("Name"), "Ben Ito")
    await userEvent.type(screen.getByLabelText("Password"), "a-long-password")
    await userEvent.click(screen.getByRole("button", { name: /Join Example Org/ }))
    const alert = await screen.findByRole("alert")
    expect(alert).toHaveTextContent("invite_used")
    expect(screen.getByLabelText("Name")).toHaveValue("Ben Ito")
  })

  it("accepts and goes into the app", async () => {
    const { calls } = mockFetch({
      "GET /api/v1/auth/invites/tok-1": preview("valid"),
      "POST /api/v1/auth/invites/tok-1/accept": json({ csrf: "c-2" }),
      "GET /api/v1/auth/status": json({ mode: "accounts", guests: false, signup_open: false }),
      "GET /api/v1/auth/me": json({ user: { id: "u-2", email: "ben@example.com", name: "Ben Ito" }, role: "member", tenant: "u-2", csrf: "c-2" }),
    })
    const { router } = renderRoutes(routes, "/invite/tok-1")
    await userEvent.type(await screen.findByLabelText("Name"), "Ben Ito")
    await userEvent.type(screen.getByLabelText("Password"), "a-long-password")
    await userEvent.click(screen.getByRole("button", { name: /Join Example Org/ }))
    await waitFor(() => expect(router.state.location.pathname).toBe("/"))
    expect(calls.find((c) => c.method === "POST")?.body).toEqual({ name: "Ben Ito", password: "a-long-password" })
  })

  it("an expired invite says so and offers sign in", async () => {
    mockFetch({ "GET /api/v1/auth/invites/tok-old": preview("expired") })
    renderRoutes(routes, "/invite/tok-old")
    expect(await screen.findByText("This invite has expired")).toBeInTheDocument()
    expect(screen.queryByLabelText("Password")).not.toBeInTheDocument()
    expect(screen.getByRole("link", { name: "Sign in instead" })).toHaveAttribute("href", "/sign-in")
  })

  it("an unknown token shows the server's code", async () => {
    mockFetch({ "GET /api/v1/auth/invites/nope": apiError(404, "not_found", "No such invite.") })
    renderRoutes(routes, "/invite/nope")
    expect(await screen.findByRole("alert")).toHaveTextContent("not_found")
  })
})
