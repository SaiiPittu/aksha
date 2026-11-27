import { render } from "@testing-library/react"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { createMemoryRouter, RouterProvider, type RouteObject } from "react-router"
import { vi } from "vitest"
import { TooltipProvider } from "@/components/ui/tooltip"

export type Call = { method: string; path: string; headers: Record<string, string>; body: unknown }
type Reply = { status?: number; body?: unknown } | Error
type Handler = (call: Call, n: number) => Reply

export const json = (body: unknown, status = 200) => ({ status, body })
export const apiError = (status: number, code: string, message: string, details: unknown = null) =>
  ({ status, body: { error: { code, message, details } } })

/** Replaces `fetch` with a router of "METHOD /api/v1/path" → reply; records every call. */
export function mockFetch(routes: Record<string, Handler | Reply>) {
  const calls: Call[] = []
  const counts: Record<string, number> = {}
  const fn = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const u = new URL(String(input), "http://localhost")
    const method = (init?.method ?? "GET").toUpperCase()
    const headers = Object.fromEntries(Object.entries((init?.headers ?? {}) as Record<string, string>))
    const body = typeof init?.body === "string" ? JSON.parse(init.body) : undefined
    const call = { method, path: u.pathname, headers, body }
    calls.push(call)
    const key = `${method} ${u.pathname}`
    counts[key] = (counts[key] ?? 0) + 1
    const route = routes[key]
    if (!route) return new Response(JSON.stringify({ error: { code: "not_found", message: key, details: null } }), { status: 404 })
    const reply = typeof route === "function" ? route(call, counts[key]) : route
    if (reply instanceof Error) throw reply
    return new Response(reply.body === undefined ? null : JSON.stringify(reply.body), {
      status: reply.status ?? 200, headers: { "Content-Type": "application/json" },
    })
  })
  vi.stubGlobal("fetch", fn)
  return { calls, fn }
}

/** Renders routes at `path` with a fresh query client; returns the router to inspect location. */
export function renderRoutes(routes: RouteObject[], path: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const router = createMemoryRouter(routes, { initialEntries: [path] })
  const utils = render(
    <QueryClientProvider client={qc}>
      <TooltipProvider><RouterProvider router={router} /></TooltipProvider>
    </QueryClientProvider>,
  )
  return { ...utils, router, qc }
}
