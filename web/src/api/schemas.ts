// Wire shapes of the responses the app consumes, as Zod schemas. Each schema accepts the
// documented fields and tolerates extra ones; list endpoints accept either a bare array or
// an object holding the array, and normalise to the array.

import { z } from "zod"

export const Role = z.enum(["owner", "admin", "member", "viewer"])
export type Role = z.infer<typeof Role>

const named = z.union([z.string(), z.object({ name: z.string().nullish(), email: z.string().nullish() }).loose()])
  .transform((v) => (typeof v === "string" ? v : v.name ?? v.email ?? ""))

function listOf<T extends z.ZodType>(key: string, item: T) {
  return z.union([z.array(item), z.object({ [key]: z.array(item) }).loose()])
    .transform((v) => (Array.isArray(v) ? v : (v as Record<string, z.output<T>[]>)[key]))
}

// ---- auth ------------------------------------------------------------------------

export const AuthStatus = z.object({
  /** "trusted": a test or legacy server that trusts a user header; it behaves like accounts. */
  mode: z.enum(["local", "accounts", "trusted"]),
  guests: z.boolean().default(false),
  signup_open: z.boolean().default(false),
}).loose()
export type AuthStatus = z.infer<typeof AuthStatus>

export const User = z.object({
  id: z.string(),
  email: z.string().nullish(),
  name: z.string().nullish(),
}).loose()

export const Org = z.object({ id: z.string(), name: z.string() }).loose()

export const Me = z.object({
  user: User.nullish(),
  org: Org.nullish(),
  role: Role,
  csrf: z.string().nullish(),
  tenant: z.string(),
  is_guest: z.boolean().default(false),
  guest_expires_at: z.string().nullish(),
}).loose()
export type Me = z.infer<typeof Me>

/** Sign-in style responses: we only need the CSRF token; /auth/me is refetched after. */
export const SignedIn = z.object({ csrf: z.string().nullish() }).loose()

export const InviteState = z.enum(["valid", "expired", "revoked", "used"])
export type InviteState = z.infer<typeof InviteState>

export const InvitePreview = z.object({
  org: named.nullish().transform((v) => v ?? ""),
  inviter: named.nullish(),
  role: Role,
  email: z.string(),
  state: InviteState,
  expires_at: z.string().nullish(),
}).loose()
export type InvitePreview = z.infer<typeof InvitePreview>

// ---- org -------------------------------------------------------------------------

export const Member = z.object({
  id: z.string().optional(),
  user_id: z.string().optional(),
  email: z.string(),
  name: z.string().nullish(),
  role: Role,
  active: z.boolean().default(true),
  last_seen_at: z.string().nullish(),
  created_at: z.string().nullish(),
}).loose().transform((m) => ({ ...m, id: m.user_id ?? m.id ?? m.email }))
export type Member = z.output<typeof Member>
export const Members = listOf("members", Member)

export const Invite = z.object({
  id: z.string(),
  email: z.string(),
  role: Role,
  state: InviteState.or(z.string()).default("valid"),
  expires_at: z.string().nullish(),
  created_at: z.string().nullish(),
  invited_by: named.nullish(),
  token: z.string().nullish(),
  link: z.string().nullish(),
  url: z.string().nullish(),
}).loose()
export type Invite = z.output<typeof Invite>
export const Invites = listOf("invites", Invite)
/** POST /org/invites returns the created invites; resend returns one (bare or wrapped). */
export const OneInvite = z.union([Invite, z.object({ invite: Invite }).loose().transform((v) => v.invite)])

export const AuditEntry = z.object({
  id: z.union([z.string(), z.number()]).optional(),
  at: z.string(),
  /** The actor's user ID (names come from the members list). */
  actor: z.string().nullish(),
  action: z.string(),
  target: z.string().nullish(),
  detail: z.unknown().optional(),
  ip: z.string().nullish(),
}).loose()
export type AuditEntry = z.output<typeof AuditEntry>
export const AuditPage = z.union([
  z.array(AuditEntry).transform((entries) => ({ entries, next: undefined as string | undefined })),
  z.object({
    records: z.array(AuditEntry).optional(),
    entries: z.array(AuditEntry).optional(),
    next: z.union([z.string(), z.number()]).nullish(),
    before: z.union([z.string(), z.number()]).nullish(),
  }).loose().transform((v) => ({
    entries: v.records ?? v.entries ?? [],
    next: (v.next ?? undefined) === undefined ? undefined : String(v.next),
  })),
])

// ---- tokens ----------------------------------------------------------------------

export const ApiToken = z.object({
  id: z.string(),
  name: z.string(),
  created_at: z.string().nullish(),
  last_used_at: z.string().nullish(),
}).loose()
export type ApiToken = z.infer<typeof ApiToken>
export const ApiTokens = listOf("tokens", ApiToken)

/** POST /tokens: the secret appears once, as `token` (a string) or `secret`. */
export const CreatedToken = z.object({
  id: z.string().optional(),
  name: z.string().optional(),
  token: z.union([z.string(), ApiToken]).optional(),
  secret: z.string().optional(),
}).loose().transform((v) => ({
  id: v.id ?? (typeof v.token === "object" ? v.token.id : undefined),
  name: v.name ?? (typeof v.token === "object" ? v.token.name : undefined),
  secret: typeof v.token === "string" ? v.token : v.secret ?? "",
}))

// ---- runtime ---------------------------------------------------------------------

export const Receipt = z.object({
  command_id: z.string(),
  seq: z.number(),
  status: z.enum(["accepted", "refused"]),
  code: z.string().nullish(),
  replayed: z.boolean().default(false),
}).loose()
export type Receipt = z.infer<typeof Receipt>
export const ReceiptBody = z.object({ receipt: Receipt }).transform((v) => v.receipt)

export const Capabilities = z.record(z.string(), z.unknown())

// ---- session reads (the reads API) -------------------------------------------------

export const Freshness = z.enum(["current", "invalidated", "dirty", "current_by_cutoff"]).or(z.string())

export const SessionRow = z.object({
  session: z.string(),
  title: z.string().nullish(),
  agent: z.string().nullish(),
  started_at: z.string().nullish(),
  last_at: z.string().nullish(),
  status: z.string().default("idle"),
  waiting: z.object({ kind: z.string(), id: z.string() }).loose().nullish(),
  run: z.string().nullish(),
  runs: z.number().nullish(),
  archived: z.boolean().default(false),
}).loose()
export type SessionRow = z.output<typeof SessionRow>
export const Sessions = listOf("sessions", SessionRow)
export const OneSession = z.union([z.object({ session: SessionRow }).loose().transform((v) => v.session), SessionRow])

/** A pending item as GET /sessions/{s} reports it (`waiting_on[]`). */
export const Waiting = z.object({
  interrupt_id: z.string().nullish(),
  kind: z.string(),
  label: z.string().nullish(),
  questions: z.array(z.union([z.tuple([z.string(), z.string()]), z.object({ id: z.string(), text: z.string() }).loose()])).nullish(),
  options: z.array(z.string()).nullish(),
  approval_id: z.string().nullish(),
  signature: z.string().nullish(),
  tool: z.string().nullish(),
  facts: z.union([z.array(z.tuple([z.string(), z.unknown()])), z.record(z.string(), z.unknown())]).nullish(),
}).loose()
export type Waiting = z.output<typeof Waiting>

const RunStatus = z.object({ run: z.string(), agent: z.string().nullish(), state: z.string(), outcome: z.string().nullish() }).loose()

export const SessionStatus = z.object({
  session: z.string(),
  run: RunStatus.nullish(),
  waiting_on: z.array(Waiting).default([]),
  seq: z.number().nullish(),
}).loose()
export type SessionStatus = z.output<typeof SessionStatus>

export const ArtifactRow = z.object({
  ref: z.string(),
  kind: z.string(),
  key: z.string().nullish(),
  version: z.number(),
  versions: z.array(z.number()).default([]),
  current: z.boolean().nullish(),
  freshness: Freshness.nullish(),
  run: z.string().nullish(),
  session: z.string().nullish(),
  saved_at: z.string().nullish(),
  media_type: z.string().nullish(),
  size: z.number().nullish(),
}).loose()
export type ArtifactRow = z.output<typeof ArtifactRow>
export const Artifacts = listOf("artifacts", ArtifactRow)

export const ArtifactDetail = ArtifactRow.extend({
  content: z.unknown(),
  writer: z.object({ agent: z.string().nullish(), task_path: z.string().nullish() }).loose().nullish(),
  reads: z.array(z.object({ id: z.string(), kind: z.string().nullish(), version: z.string().nullish() }).loose()).default([]),
  primary_inputs: z.array(z.string()).default([]),
  citations: z.array(z.object({
    n: z.number().nullish(), pointer: z.string().nullish(), quote: z.string().nullish(), target: z.string().nullish(),
    record_id: z.string().nullish(), form: z.string().nullish(),
  }).loose()).default([]),
}).loose()
export type ArtifactDetail = z.output<typeof ArtifactDetail>

export const ArtifactDiff = z.object({
  before: z.unknown(),
  after: z.unknown(),
  changes: z.array(z.object({ pointer: z.string().nullish(), op: z.string(), before: z.unknown(), after: z.unknown() }).loose()).default([]),
}).loose()
export type ArtifactDiff = z.output<typeof ArtifactDiff>

export const Lineage = z.object({
  nodes: z.array(z.object({ id: z.string(), type: z.string().default("artifact"), label: z.string().nullish(), freshness: Freshness.nullish() }).loose()).default([]),
  edges: z.array(z.object({ from: z.string(), to: z.string() }).loose()).default([]),
}).loose()
export type Lineage = z.output<typeof Lineage>

export const DocumentRow = z.object({
  doc_key: z.string(),
  path: z.string(),
  name: z.string().nullish(),
  version: z.number().default(1),
  versions: z.array(z.number()).default([]),
  media_type: z.string().nullish(),
  size: z.number().nullish(),
  stored_at: z.string().nullish(),
  origin: z.string().nullish(),
  /** In an upload's response: new, changed or unchanged. */
  change: z.string().nullish(),
  status: z.string().nullish(),
  freshness: Freshness.nullish(),
}).loose()
export type DocumentRow = z.output<typeof DocumentRow>
export const Documents = listOf("documents", DocumentRow)

/** POST /inputs/upload: the receipt plus what this upload stored. */
export const UploadResponse = z.object({ receipt: Receipt, documents: z.array(DocumentRow).default([]) }).loose()
export type UploadResponse = z.output<typeof UploadResponse>
