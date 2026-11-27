import { describe, expect, it } from "vitest"
import { ApiError, type AuthStatus, type Me } from "@/api"
import { decideApp, decidePublic, safeNext } from "./guard"

const accounts: AuthStatus = { mode: "accounts", guests: false, signup_open: false }
const local: AuthStatus = { mode: "local", guests: false, signup_open: false }
const fresh: AuthStatus = { mode: "accounts", guests: false, signup_open: true }
const me: Me = { user: { id: "u-1", email: "ana@example.com", name: "Ana" }, org: { id: "o-1", name: "Example Org" },
  role: "member", csrf: "c", tenant: "u-1", is_guest: false, guest_expires_at: null }
const guest: Me = { ...me, is_guest: true, tenant: "g-1" }

describe("decideApp", () => {
  it("local mode goes straight in, whatever /auth/me says", () => {
    expect(decideApp({ status: local, me: null, path: "/" })).toEqual({ kind: "allow", mode: "local" })
    expect(decideApp({ status: local, me: undefined, path: "/settings/members" })).toEqual({ kind: "allow", mode: "local" })
  })
  it("signed out goes to sign in, keeping where they were going", () => {
    expect(decideApp({ status: accounts, me: null, path: "/s/S-1?view=artifact:a@v1" }))
      .toEqual({ kind: "redirect", to: "/sign-in?next=%2Fs%2FS-1%3Fview%3Dartifact%3Aa%40v1" })
    expect(decideApp({ status: accounts, me: null, path: "/" })).toEqual({ kind: "redirect", to: "/sign-in" })
  })
  it("signed in is allowed in, guests as guests", () => {
    expect(decideApp({ status: accounts, me, path: "/" })).toEqual({ kind: "allow", mode: "account" })
    expect(decideApp({ status: accounts, me: guest, path: "/" })).toEqual({ kind: "allow", mode: "guest" })
  })
  it("a fresh server sends the first visitor to sign up", () => {
    expect(decideApp({ status: fresh, me: null, path: "/" })).toEqual({ kind: "redirect", to: "/sign-up" })
    const setup = new ApiError(401, "setup_required", "no account exists yet")
    expect(decideApp({ status: accounts, me: undefined, error: setup, path: "/" })).toEqual({ kind: "redirect", to: "/sign-up" })
  })
  it("waits while loading, and reports other failures", () => {
    expect(decideApp({ status: undefined, me: undefined, path: "/" })).toEqual({ kind: "loading" })
    expect(decideApp({ status: accounts, me: undefined, path: "/" })).toEqual({ kind: "loading" })
    const down = new ApiError(0, "network", "unreachable")
    expect(decideApp({ status: undefined, me: undefined, error: down, path: "/" })).toEqual({ kind: "error", error: down })
  })
})

describe("decidePublic", () => {
  it("sends signed-in visitors and local mode to the app", () => {
    expect(decidePublic({ status: accounts, me, path: "/sign-in" }, "sign-in")).toEqual({ kind: "redirect", to: "/" })
    expect(decidePublic({ status: local, me: null, path: "/sign-in" }, "sign-in")).toEqual({ kind: "redirect", to: "/" })
  })
  it("shows sign in when signed out, and sign up only while sign-up is open", () => {
    expect(decidePublic({ status: accounts, me: null, path: "/sign-in" }, "sign-in")).toEqual({ kind: "allow", mode: "account" })
    expect(decidePublic({ status: accounts, me: null, path: "/sign-up" }, "sign-up")).toEqual({ kind: "redirect", to: "/sign-in" })
    expect(decidePublic({ status: fresh, me: null, path: "/sign-up" }, "sign-up")).toEqual({ kind: "allow", mode: "account" })
    expect(decidePublic({ status: fresh, me: null, path: "/sign-in" }, "sign-in")).toEqual({ kind: "redirect", to: "/sign-up" })
  })
  it("shows try only when the server allows guests", () => {
    expect(decidePublic({ status: accounts, me: null, path: "/try" }, "try")).toEqual({ kind: "redirect", to: "/sign-in" })
    expect(decidePublic({ status: { ...accounts, guests: true }, me: null, path: "/try" }, "try")).toEqual({ kind: "allow", mode: "account" })
  })
})

describe("safeNext", () => {
  it("follows only same-app paths", () => {
    expect(safeNext("/s/S-1")).toBe("/s/S-1")
    expect(safeNext("//evil.example")).toBe("/")
    expect(safeNext("https://evil.example")).toBe("/")
    expect(safeNext(null)).toBe("/")
  })
})
