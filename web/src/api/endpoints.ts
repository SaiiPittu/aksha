// Every route the web app calls, with its request and response shapes.

import { API_BASE, command, newCommandId, request, requestText, setCsrfToken, upload, type UploadPart } from "./client"
import * as S from "./schemas"

const enc = encodeURIComponent

/** Absolute invite link for a token (the server hands out tokens; links point at this app). */
export function inviteLink(i: { token?: string | null; link?: string | null; url?: string | null }): string | undefined {
  const raw = i.link ?? i.url ?? (i.token ? `/invite/${enc(i.token)}` : undefined)
  if (!raw) return undefined
  return raw.startsWith("http") ? raw : `${window.location.origin}${raw.startsWith("/") ? "" : "/"}${raw}`
}

async function signedIn<T extends { csrf?: string | null }>(p: Promise<T>): Promise<T> {
  const r = await p
  if (r?.csrf) setCsrfToken(r.csrf)
  return r
}

export const auth = {
  status: () => request("GET", "/auth/status", { schema: S.AuthStatus }),
  me: async () => {
    const me = await request("GET", "/auth/me", { schema: S.Me })
    setCsrfToken(me.csrf)
    return me
  },
  login: (body: { email: string; password: string; remember: boolean }) =>
    signedIn(request("POST", "/auth/login", { body, schema: S.SignedIn })),
  logout: async () => {
    try { await request("POST", "/auth/logout") } finally { setCsrfToken(null) }
  },
  signup: (body: { name: string; email: string; password: string; org_name: string }) =>
    signedIn(request("POST", "/auth/signup", { body, schema: S.SignedIn })),
  claim: (body: { name: string; email: string; password: string; org_name: string }) =>
    signedIn(request("POST", "/auth/claim", { body, schema: S.SignedIn })),
  changePassword: (body: { current: string; new: string }) =>
    request("POST", "/auth/password", { body }),
  invitePreview: (token: string) => request("GET", `/auth/invites/${enc(token)}`, { schema: S.InvitePreview }),
  acceptInvite: (token: string, body: { name: string; password: string }) =>
    signedIn(request("POST", `/auth/invites/${enc(token)}/accept`, { body, schema: S.SignedIn })),
  guest: () => signedIn(request("POST", "/auth/guest", { body: {}, schema: S.SignedIn })),
  saveGuest: (body: { name: string; email: string; password: string }) =>
    signedIn(request("POST", "/auth/guest/save", { body, schema: S.SignedIn })),
}

export const org = {
  members: () => request("GET", "/org/members", { schema: S.Members }),
  updateMember: (user: string, body: { role?: S.Role; active?: boolean }) =>
    request("PATCH", `/org/members/${enc(user)}`, { body }),
  invites: () => request("GET", "/org/invites", { schema: S.Invites }),
  createInvites: (body: { emails: string[]; role: S.Role }) =>
    request("POST", "/org/invites", { body, schema: S.Invites }),
  resendInvite: (id: string) => request("POST", `/org/invites/${enc(id)}/resend`, { body: {}, schema: S.OneInvite }),
  revokeInvite: (id: string) => request("DELETE", `/org/invites/${enc(id)}`),
  audit: (q: { limit?: number; before?: string; action?: string; actor?: string }) =>
    request("GET", "/org/audit", { query: q, schema: S.AuditPage }),
}

export const tokens = {
  list: () => request("GET", "/tokens", { schema: S.ApiTokens }),
  create: (body: { name: string }) => request("POST", "/tokens", { body, schema: S.CreatedToken }),
  revoke: (id: string) => request("DELETE", `/tokens/${enc(id)}`),
}

export const runtime = {
  capabilities: () => request("GET", "/capabilities", { schema: S.Capabilities }),
  verify: () => request("GET", "/verify"),
}

/**
 * Session commands. Each returns a `Command`: make one per click, call `send()`; a retry
 * of that click calls `send()` on the same object, so the command ID stays the same.
 */
export const commands = {
  sendMessage: (session: string, body: { text: string; agent?: string }) =>
    command(`/sessions/${enc(session)}/messages`, body, S.ReceiptBody),
  answer: (interrupt: string, body: { text?: string; answers?: Record<string, string>; agent?: string }) =>
    command(`/interrupts/${enc(interrupt)}/answer`, body, S.ReceiptBody),
  approve: (approval: string, shownSignature: string) =>
    command(`/approvals/${enc(approval)}/approve`, { shown_signature: shownSignature }, S.ReceiptBody),
  reject: (approval: string, note?: string) =>
    command(`/approvals/${enc(approval)}/reject`, note ? { note } : {}, S.ReceiptBody),
  stop: (run: string, reason?: string) =>
    command(`/runs/${enc(run)}/stop`, reason ? { reason } : {}, S.ReceiptBody),
  steer: (run: string, body: { text: string; lane?: string }) =>
    command(`/runs/${enc(run)}/steer`, body, S.ReceiptBody),
  acceptReview: (review: string, cardHash: string) =>
    command(`/reviews/${enc(review)}/accept`, { card_hash: cardHash }, S.ReceiptBody),
  rejectReview: (review: string, cardHash?: string) =>
    command(`/reviews/${enc(review)}/reject`, cardHash ? { card_hash: cardHash } : {}, S.ReceiptBody),
  grantBudget: (interrupt: string, extra: Record<string, number>) =>
    command("/commands", { command: "GrantBudget", args: { interrupt_id: interrupt, extra } }, S.ReceiptBody),
  finalize: (interrupt: string) =>
    command("/commands", { command: "Finalize", args: { interrupt_id: interrupt } }, S.ReceiptBody),
  /** Any named command, for kinds without a dedicated route (budget, review and so on). */
  named: (name: string, args: Record<string, unknown>) =>
    command("/commands", { command: name, args }, S.ReceiptBody),
}

/** The SSE stream URL for a session (the session store opens it with EventSource). */
export const sessionEventsUrl = (session: string, after?: number) =>
  `${API_BASE}/sessions/${enc(session)}/events${after != null ? `?after=${after}` : ""}`

/** Uploads files (multipart, one `file` part per file named by its relative path). */
export const uploadInputs = (parts: UploadPart[], onProgress?: (loaded: number, total: number) => void) =>
  upload("/inputs/upload", parts, { command_id: newCommandId() }, S.UploadResponse, onProgress)

/** Session, artifact and document reads (the reads API). */
export const reads = {
  session: (session: string) => request("GET", `/sessions/${enc(session)}`, { schema: S.SessionStatus }),
  sessions: (archived = false) => request("GET", "/sessions", { query: { archived: archived ? 1 : 0 }, schema: S.Sessions }),
  archive: (session: string) => request("POST", `/sessions/${enc(session)}/archive`, { body: {}, schema: S.OneSession }),
  restore: (session: string) => request("POST", `/sessions/${enc(session)}/restore`, { body: {}, schema: S.OneSession }),
  rename: (session: string, title: string) => request("PATCH", `/sessions/${enc(session)}`, { body: { title }, schema: S.OneSession }),
  artifacts: (q: { session?: string; run?: string } = {}) => request("GET", "/artifacts", { query: q, schema: S.Artifacts }),
  artifact: (ref: string) => request("GET", `/artifacts/${enc(ref)}`, { schema: S.ArtifactDetail }),
  artifactDiff: (ref: string, against: string) =>
    request("GET", `/artifacts/${enc(ref)}/diff`, { query: { against: against.startsWith("@") ? against : `@${against}` }, schema: S.ArtifactDiff }),
  lineage: (ref: string, dir: "up" | "down", depth = 3) =>
    request("GET", `/artifacts/${enc(ref)}/lineage`, { query: { dir, depth }, schema: S.Lineage }),
  documents: () => request("GET", "/documents", { schema: S.Documents }),
  document: (docKey: string, version?: number) => requestText(`/documents/${enc(docKey)}`, { version }),
}
