import { screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { afterEach, describe, expect, it, vi } from "vitest"
import { apiError, json, mockFetch, renderRoutes } from "@/test/utils"
import { SignIn } from "./SignIn"

afterEach(() => vi.unstubAllGlobals())

const status = json({ mode: "accounts", guests: true, signup_open: false })
const routes = [{ path: "/sign-in", element: <SignIn /> }, { path: "/s/:session", element: <p>chat screen</p> }]

describe("SignIn", () => {
  it("checks the fields before sending anything", async () => {
    const { calls } = mockFetch({ "GET /api/v1/auth/status": status })
    renderRoutes(routes, "/sign-in")
    await userEvent.click(screen.getByRole("button", { name: /^Sign in/ }))
    expect(screen.getByText("Enter your email.")).toBeInTheDocument()
    expect(screen.getByText("Enter your password.")).toBeInTheDocument()
    await userEvent.type(screen.getByLabelText("Email"), "not-an-email")
    await userEvent.click(screen.getByRole("button", { name: /^Sign in/ }))
    expect(screen.getByText("Enter a valid email address.")).toBeInTheDocument()
    expect(calls.filter((c) => c.method === "POST")).toHaveLength(0)
  })

  it("shows the server's refusal inline with its code", async () => {
    mockFetch({
      "GET /api/v1/auth/status": status,
      "POST /api/v1/auth/login": apiError(401, "invalid_credentials", "Wrong email or password."),
    })
    renderRoutes(routes, "/sign-in")
    await userEvent.type(screen.getByLabelText("Email"), "ana@example.com")
    await userEvent.type(screen.getByLabelText("Password"), "wrong-password")
    await userEvent.click(screen.getByRole("button", { name: /^Sign in/ }))
    const alert = await screen.findByRole("alert")
    expect(alert).toHaveTextContent("invalid_credentials")
    expect(alert).toHaveTextContent("Wrong email or password.")
  })

  it("signs in and follows ?next=", async () => {
    const { calls } = mockFetch({
      "GET /api/v1/auth/status": status,
      "POST /api/v1/auth/login": json({ csrf: "c-1" }),
      "GET /api/v1/auth/me": json({ user: { id: "u-1", email: "ana@example.com", name: "Ana" }, org: { id: "o", name: "Org" }, role: "member", csrf: "c-1", tenant: "u-1", is_guest: false }),
    })
    const { router } = renderRoutes(routes, "/sign-in?next=%2Fs%2FS-0001")
    await userEvent.type(screen.getByLabelText("Email"), "ana@example.com")
    await userEvent.type(screen.getByLabelText("Password"), "a-long-password")
    await userEvent.click(screen.getByRole("button", { name: /^Sign in/ }))
    await waitFor(() => expect(router.state.location.pathname).toBe("/s/S-0001"))
    expect(calls.find((c) => c.path === "/api/v1/auth/login")?.body).toEqual({ email: "ana@example.com", password: "a-long-password", remember: true })
  })

  it("offers the guest sandbox only when the server allows it", async () => {
    mockFetch({ "GET /api/v1/auth/status": status })
    renderRoutes(routes, "/sign-in")
    expect(await screen.findByRole("link", { name: /Try without an account/ })).toBeInTheDocument()
  })
})
