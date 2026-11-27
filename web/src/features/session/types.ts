// The session view model: what the chat screen renders. Agent-neutral.
//
// The live session store (SSE client + reducer over runtime events) produces a
// `SessionState`; the screens only read it. Nothing here knows about the wire: the
// reducer maps runtime events into these shapes.

export type Status = "wait" | "run" | "ok" | "bad" | "idle"

export type ViewKind =
  | "markdown" | "pdf" | "text" | "json" | "table" | "diff" | "image"
  | "web" | "embed" | "html" | "ui-spec" | "trace" | `plugin:${string}`

/** Anything the view panel can open: an input file, an artifact, a URL, a citation, a tool result, a run. */
export interface ViewRef {
  /** Stable key for tabs and for the `?view=` URL param, e.g. "artifact:summary@v2". */
  id: string
  kind: ViewKind
  title: string
  source: "input" | "artifact" | "url" | "citation" | "tool" | "run"
  version?: string
  versions?: string[]
  /** An artifact's version ref (`kind[/key]@vN`), the key for GET /artifacts/{ref}. */
  ref?: string
  /** An uploaded document's key, for GET /documents/{doc_key}. */
  docKey?: string
  mediaType?: string
  freshness?: Freshness
  /** Citations bound to this artifact version (citation.bound). */
  citations?: ArtifactCitation[]
  // Payloads per kind (only the one matching `kind` is set). The store may leave them
  // empty and fill them on open; renderers show a "nothing to show" state meanwhile.
  markdown?: string
  text?: { language: string; body: string }
  /** Parsed JSON content (an artifact or a .json input). */
  json?: unknown
  table?: { columns: string[]; rows: (string | number)[][]; provenance?: Record<string, string> }
  diff?: { mode: "text" | "table"; before: string; after: string; label: string }
  url?: string
  html?: string
  spec?: UiSpecNode
  pages?: string[][]
  highlight?: string
  embedProvider?: string
  spans?: TraceSpan[]
}

export type Freshness = "current" | "invalidated" | "dirty" | "current_by_cutoff"

export interface ArtifactCitation { pointer?: string; target?: string; quote?: string; form?: string; recordId?: string }

export interface UiSpecNode {
  type: "card" | "stack" | "text" | "metric" | "table" | "button" | "badge" | "chart-bar"
  props?: Record<string, unknown>
  children?: UiSpecNode[]
}

export type TraceSpanType = "turn" | "model" | "tool" | "interrupt"
export interface TraceSpan {
  id: string
  parent?: string
  type: TraceSpanType
  name: string
  /** Milliseconds from the run's start. */
  start: number
  dur: number
  status?: "ok" | "bad" | "wait"
  attrs: [string, string][]
}

export interface Citation { n: number; label: string; view: ViewRef }

export type ToolPart = {
  type: "tool"; name: string; summary: string; state: "running" | "done" | "refused" | "error"
  durationMs?: number; input?: string; output?: string; refusal?: string; view?: ViewRef
}

export type Part =
  | { type: "text"; text: string; citations?: Citation[]; streaming?: boolean }
  | ToolPart
  /** One Task's steps; `saved` lists the artifacts the Task saved, shown even when collapsed. */
  | { type: "step-group"; label: string; steps: ToolPart[]; saved?: ViewRef[]; done?: boolean }
  | { type: "artifact"; view: ViewRef; note: string }
  | { type: "files"; files: ViewRef[] }
  /** Interrupts live in `SessionState.interrupts`; a message part only points at one. */
  | { type: "interrupt"; interruptId: string }
  | { type: "ui-spec"; spec: UiSpecNode }
  | { type: "notice"; tone: Status; text: string }

export interface Message {
  id: string
  role: "user" | "agent" | "system"
  /** The user's name, or the agent label (from route.decided). */
  author: string
  /** ISO timestamp. */
  at: string
  parts: Part[]
  /** A message sent into an open run: its lane, and whether the run has read it yet. */
  lane?: Lane
  laneState?: "queued" | "consumed"
  /** Optimistic: shown before the stream confirms it (keyed by command ID). */
  pending?: boolean
}

export type InterruptKind = "input" | "choice" | "approval" | "budget" | "review" | "route"
export type InterruptState =
  | "open" | "submitting" | "resolved" | "resolved-elsewhere" | "voided" | "expired"
  | "granted" | "rejected" | "refused"

export interface Interrupt {
  /** "I-0002"; approvals "A-0003". */
  id: string
  runId?: string
  kind: InterruptKind
  state: InterruptState
  title: string
  detail?: string
  questions?: {
    id: string; text: string; options?: { key: string; label: string; hint?: string }[]
    selected?: string; freeText?: boolean
  }[]
  answer?: string
  decidedBy?: string
  refusal?: { code: string; message: string }
  action?: {
    tool: string; leavesSystem: boolean; facts: [string, string][]; payloadPreview?: string
    payloadView?: ViewRef; signature: string; expiresIn?: string
    /** The exact artifact versions the action acts on. */
    targets?: string[]
    /** Set once the action left the system (outbox completed). */
    delivered?: string
  }
  budget?: { used: string; limit: string; options: ("grant" | "finalize" | "stop")[]; dimension?: string; allowed?: number }
  review?: { artifact: string; from: string; to: string; changes: number; view?: ViewRef; cardHash?: string; old?: string; revision?: string }
}

export interface Run {
  id: string
  agent: string
  status: Status
  /** The chip text, e.g. "Running", "Waiting on you", "Stop requested". */
  label: string
  turns: string
  tokens: string
  elapsed: string
  plan?: { name: string; status: Status; note?: string }[]
  /** For a rebuild run: the runs it reworks. */
  forRuns?: string[]
  outcome?: string
}

export type PanelMode = "closed" | "peek" | "pinned" | "fullscreen"
export interface PanelState { mode: PanelMode; tabs: ViewRef[]; active?: string; followAgent?: boolean }

/** M6: the stream connection, as the banner shows it. */
export type StreamState = "connecting" | "live" | "replaying" | "reconnecting" | "refetching"

export type Lane = "steer" | "follow_up" | "amend"

/** Everything the chat screen needs about one session. */
export interface SessionState {
  sessionId: string | null
  /** "idle" before any data; "ready" once the first synchronized marker landed. */
  phase: "idle" | "loading" | "ready" | "not_found" | "error"
  title: string
  /** The agent named for this session, if any (else the header says Auto-route). */
  agent?: string
  messages: Message[]
  /** All runs of the session, oldest first. The header shows the last open one. */
  runs: Run[]
  /** Open and closed interrupts by ID, so a card can update in place. */
  interrupts: Record<string, Interrupt>
  /** Inputs and artifacts the session knows, by view ID. */
  artifacts: ViewRef[]
  /** A view the agent suggested last; the panel opens it only under Follow agent. */
  suggestedView?: string
  stream: StreamState
  /** The last applied event seq (the SSE cursor). */
  cursor: number | null
  /** When the stream fails for good: the server's error code. */
  error?: { code: string; message: string }
}

// ---- commands the chat screen issues ------------------------------------------

export interface SendInput { text: string; lane?: Lane; agent?: string; files?: ViewRef[] }

export type InterruptAction =
  | { kind: "answer"; text?: string; answers?: Record<string, string>; agent?: string }
  | { kind: "skip" }
  | { kind: "approve"; signature: string }
  | { kind: "reject"; note?: string }
  | { kind: "grant" }
  | { kind: "finalize" }
  | { kind: "stop" }
  | { kind: "accept-review" }
  | { kind: "reject-review" }

/**
 * Commands the screen calls. Each returns when the server's receipt is accepted and
 * rejects with an `ApiError` (code shown inline) when refused. The effect arrives on the
 * stream, never from the promise.
 */
export interface SessionCommands {
  send(input: SendInput): Promise<void>
  act(interruptId: string, action: InterruptAction): Promise<void>
  stop(runId: string): Promise<void>
  /** Uploads files (attach or drop); reports per-file progress; resolves with what was stored. */
  upload?(files: File[], onProgress?: (p: UploadProgress[]) => void): Promise<UploadedFile[]>
}

export interface UploadProgress { path: string; loaded: number; total: number }

/** One file an upload stored, as the composer chip shows it. */
export interface UploadedFile {
  path: string
  view: ViewRef
  /** "changed" when the path existed with other bytes, "unchanged" for the same bytes. */
  change: "new" | "changed" | "unchanged"
  from?: number
  to: number
}

export interface SessionHandle {
  state: SessionState
  commands: SessionCommands
}

/** One row of the left rail and the sessions page. */
export interface SessionSummary {
  id: string
  title: string
  agent: string
  status: Status
  statusLabel: string
  age: string
  owner?: string
  /** The pending item, when the session waits on a person. */
  waiting?: { kind: string; id: string }
  archived?: boolean
  run?: string
  lastAt?: string
}
