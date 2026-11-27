// M1 App and auth: which screen a route may show, from GET /auth/status and GET /auth/me.
// Pure, so every decision has a unit test.

import type { ApiError, AuthStatus, Me } from "@/api"

export type GuardInput = {
  status: AuthStatus | undefined
  /** `undefined` while loading; `null` when /auth/me said 401. */
  me: Me | null | undefined
  /** A failed /auth/me other than 401 (or a failed /auth/status). */
  error?: ApiError | Error | null
  /** The path the user asked for, kept as `?next=` on a redirect to sign in. */
  path: string
}

export type GuardDecision =
  | { kind: "loading" }
  | { kind: "error"; error: ApiError | Error }
  | { kind: "allow"; mode: "local" | "account" | "guest" }
  | { kind: "redirect"; to: string }

const next = (path: string) => (path && path !== "/" ? `?next=${encodeURIComponent(path)}` : "")

/** For app routes (chat, settings, pages). */
export function decideApp({ status, me, error, path }: GuardInput): GuardDecision {
  if (status?.mode === "local") return { kind: "allow", mode: "local" }
  if (error && me === undefined) {
    // A fresh server reached from another machine: the only way in is the first sign-up.
    if ("code" in error && error.code === "setup_required") return { kind: "redirect", to: "/sign-up" }
    return { kind: "error", error }
  }
  if (status === undefined || me === undefined) return { kind: "loading" }
  if (me) return { kind: "allow", mode: me.is_guest ? "guest" : "account" }
  if (status.signup_open) return { kind: "redirect", to: "/sign-up" }
  return { kind: "redirect", to: `/sign-in${next(path)}` }
}

/** For the signed-out screens (sign in, sign up): a signed-in visitor goes to the app. */
export function decidePublic({ status, me, error }: GuardInput, page: "sign-in" | "sign-up" | "try"): GuardDecision {
  if (status?.mode === "local") return { kind: "redirect", to: "/" }
  if (error && me === undefined) {
    if ("code" in error && error.code === "setup_required" && page === "sign-up") return { kind: "allow", mode: "account" }
    if ("code" in error && error.code === "setup_required") return { kind: "redirect", to: "/sign-up" }
    return { kind: "allow", mode: "account" }
  }
  if (status === undefined || me === undefined) return { kind: "loading" }
  if (me && !(page === "try" && me.is_guest)) return { kind: "redirect", to: "/" }
  if (page === "sign-up" && !status.signup_open) return { kind: "redirect", to: "/sign-in" }
  if (page === "sign-in" && status.signup_open) return { kind: "redirect", to: "/sign-up" }
  if (page === "try" && !status.guests) return { kind: "redirect", to: "/sign-in" }
  return { kind: "allow", mode: "account" }
}

/** Only same-app paths are followed after sign-in. */
export function safeNext(raw: string | null): string {
  if (!raw || !raw.startsWith("/") || raw.startsWith("//")) return "/"
  return raw
}
