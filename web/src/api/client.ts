// The HTTP client. Every request in the app goes through `request` (reads) or a
// `Command` (writes that carry a command ID). Nothing outside src/api/ builds a URL.

import type { z } from "zod"

export const API_BASE = "/api/v1"

/** The server's one error shape, `{error: {code, message, details}}`, as a typed error. */
export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly details: unknown

  constructor(status: number, code: string, message: string, details?: unknown) {
    super(message)
    this.name = "ApiError"
    this.status = status
    this.code = code
    this.details = details
  }

  /** True for failures where the same request may succeed when sent again. */
  get retryable(): boolean {
    return this.code === "network" || this.status === 502 || this.status === 503 || this.status === 504
  }
}

export const isApiError = (e: unknown): e is ApiError => e instanceof ApiError

const UNSAFE = new Set(["POST", "PUT", "PATCH", "DELETE"])

// The CSRF token of the current cookie session (from /auth/me or /auth/login).
let csrfToken: string | null = null
export function setCsrfToken(token: string | null | undefined) { csrfToken = token ?? null }
export function getCsrfToken() { return csrfToken }

export type Method = "GET" | "POST" | "PUT" | "PATCH" | "DELETE"
export type Query = Record<string, string | number | boolean | null | undefined>

export interface RequestOptions<S extends z.ZodType | undefined> {
  body?: unknown
  query?: Query
  schema?: S
  signal?: AbortSignal
}

function url(path: string, query?: Query): string {
  const q = new URLSearchParams()
  for (const [k, v] of Object.entries(query ?? {})) if (v !== undefined && v !== null && v !== "") q.set(k, String(v))
  const qs = q.toString()
  return `${API_BASE}${path}${qs ? `?${qs}` : ""}`
}

async function errorFrom(res: Response): Promise<ApiError> {
  let data: unknown
  try { data = await res.json() } catch { data = undefined }
  const e = (data as { error?: { code?: unknown; message?: unknown; details?: unknown } } | undefined)?.error
  if (e && typeof e.code === "string") {
    return new ApiError(res.status, e.code, typeof e.message === "string" ? e.message : e.code, e.details)
  }
  // FastAPI's own 422 shape, or a proxy page: keep the status visible.
  const detail = (data as { detail?: unknown } | undefined)?.detail
  return new ApiError(res.status, `http_${res.status}`, typeof detail === "string" ? detail : res.statusText || `HTTP ${res.status}`, data)
}

type Out<S> = S extends z.ZodType ? z.output<S> : unknown

/** One JSON request. Resolves with the parsed body, or rejects with an `ApiError`. */
export async function request<S extends z.ZodType | undefined = undefined>(
  method: Method, path: string, opts: RequestOptions<S> = {},
): Promise<Out<S>> {
  const headers: Record<string, string> = { Accept: "application/json" }
  if (opts.body !== undefined) headers["Content-Type"] = "application/json"
  if (UNSAFE.has(method) && csrfToken) headers["X-CSRF-Token"] = csrfToken
  let res: Response
  try {
    res = await fetch(url(path, opts.query), {
      method,
      headers,
      credentials: "include",
      body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
      signal: opts.signal,
    })
  } catch (e) {
    if ((e as Error)?.name === "AbortError") throw e
    throw new ApiError(0, "network", "The server could not be reached.")
  }
  if (!res.ok) throw await errorFrom(res)
  if (res.status === 204) return undefined as Out<S>
  let data: unknown
  try { data = await res.json() } catch { data = undefined }
  if (!opts.schema) return data as Out<S>
  const parsed = opts.schema.safeParse(data)
  if (!parsed.success) {
    throw new ApiError(res.status, "bad_response", `Unexpected response from ${method} ${path}`, parsed.error.issues)
  }
  return parsed.data as Out<S>
}

/** A raw read (a stored document's bytes as text), with the media type the server stored. */
export async function requestText(path: string, query?: Query): Promise<{ text: string; mediaType: string }> {
  let res: Response
  try {
    res = await fetch(url(path, query), { credentials: "include", headers: { Accept: "*/*" } })
  } catch {
    throw new ApiError(0, "network", "The server could not be reached.")
  }
  if (!res.ok) throw await errorFrom(res)
  return { text: await res.text(), mediaType: (res.headers.get("Content-Type") ?? "").split(";")[0].trim() }
}

export interface UploadPart { file: Blob; path: string }

/**
 * A multipart upload (POST), with progress: `onProgress(loaded, total)` in body bytes. Uses
 * XMLHttpRequest because fetch reports no upload progress. Rejects with an `ApiError`.
 */
export function upload<S extends z.ZodType>(path: string, parts: UploadPart[], fields: Record<string, string>, schema: S,
  onProgress?: (loaded: number, total: number) => void): Promise<z.output<S>> {
  const form = new FormData()
  for (const [k, v] of Object.entries(fields)) form.append(k, v)
  for (const p of parts) form.append("file", p.file, p.path)
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    xhr.open("POST", url(path))
    xhr.withCredentials = true
    xhr.setRequestHeader("Accept", "application/json")
    if (csrfToken) xhr.setRequestHeader("X-CSRF-Token", csrfToken)
    xhr.upload.onprogress = (e) => { if (e.lengthComputable) onProgress?.(e.loaded, e.total) }
    xhr.onerror = () => reject(new ApiError(0, "network", "The server could not be reached."))
    xhr.onload = () => {
      let data: unknown
      try { data = JSON.parse(xhr.responseText) } catch { data = undefined }
      if (xhr.status < 200 || xhr.status >= 300) {
        const e = (data as { error?: { code?: unknown; message?: unknown; details?: unknown } } | undefined)?.error
        reject(e && typeof e.code === "string"
          ? new ApiError(xhr.status, e.code, typeof e.message === "string" ? e.message : e.code, e.details)
          : new ApiError(xhr.status, `http_${xhr.status}`, xhr.statusText || `HTTP ${xhr.status}`, data))
        return
      }
      const parsed = schema.safeParse(data)
      if (!parsed.success) reject(new ApiError(xhr.status, "bad_response", `Unexpected response from POST ${path}`, parsed.error.issues))
      else resolve(parsed.data)
    }
    xhr.send(form)
  })
}

// ---- commands -------------------------------------------------------------------

/** A fresh command ID: one per click, reused for that click's retries. */
export const newCommandId = () => `c-${crypto.randomUUID()}`

export interface CommandOptions {
  /** Automatic retries for network failures and 502/503/504 (same command ID). Default 2. */
  retries?: number
  /** Delay before retry n (1-based), in ms. */
  backoff?: (attempt: number) => number
}

/**
 * One user action that writes. The command ID is fixed when the command is made, so
 * every send (automatic retries, or a "Retry" button calling `send()` again) carries the
 * same ID and the server can answer a duplicate with the stored receipt (`replayed`).
 */
export class Command<S extends z.ZodType | undefined = undefined> {
  readonly id: string
  readonly method: Method
  readonly path: string
  private readonly body: Record<string, unknown>
  private readonly schema?: S
  private readonly opts: CommandOptions

  constructor(method: Method, path: string, body: Record<string, unknown>, schema?: S, opts: CommandOptions = {}) {
    this.id = newCommandId()
    this.method = method
    this.path = path
    this.body = body
    this.schema = schema
    this.opts = opts
  }

  async send(): Promise<Out<S>> {
    const retries = this.opts.retries ?? 2
    const backoff = this.opts.backoff ?? ((n: number) => 250 * 2 ** (n - 1))
    for (let attempt = 0; ; attempt++) {
      try {
        return await request(this.method, this.path, { body: { ...this.body, command_id: this.id }, schema: this.schema })
      } catch (e) {
        if (!isApiError(e) || !e.retryable || attempt >= retries) throw e
        await new Promise((r) => setTimeout(r, backoff(attempt + 1)))
      }
    }
  }
}

export function command<S extends z.ZodType | undefined = undefined>(
  path: string, body: Record<string, unknown> = {}, schema?: S, opts?: CommandOptions,
): Command<S> {
  return new Command("POST", path, body, schema, opts)
}
