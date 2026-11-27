import { ApiError, endpoints, isApiError, type DocumentRow } from "@/api"
import type { InterruptAction, SendInput, SessionCommands, SessionState, UploadedFile, UploadProgress } from "./types"
import type { LocalAction } from "./wire"
import { openRun } from "./selectors"
import { documentViewId, kindForMedia } from "./reducer"

const notYet = (what: string) => Promise.reject(new ApiError(0, "not_supported", `${what} is not available on this server.`))

/** A fresh session ID: the client names the session; its first message creates it. */
export function newSessionId(): string {
  const b = new Uint8Array(6)
  crypto.getRandomValues(b)
  return `S-${Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("")}`
}

/**
 * The path a file uploads under: its name, or for a file from a picked folder its path
 * inside that folder (the folder's own name dropped), as `aksha run --input DIR` does.
 */
export function uploadPath(f: File): string {
  const rel = (f as File & { webkitRelativePath?: string }).webkitRelativePath
  if (!rel) return f.name
  const cut = rel.indexOf("/")
  return cut >= 0 ? rel.slice(cut + 1) : rel
}

export interface CommandContext {
  getState: () => SessionState
  dispatch: (a: LocalAction) => void
  /** A first message created a session: show it (the store for it seeds with `seed`). */
  onNewSession?: (sessionId: string, seed: LocalAction) => void
  now?: () => string
}

/** The view a stored document opens as. */
export function documentUpload(d: DocumentRow): UploadedFile {
  const versions = d.versions.length ? d.versions : Array.from({ length: d.version }, (_, i) => i + 1)
  const change = (d.change ?? d.status ?? (d.version > 1 ? "changed" : "new")) as UploadedFile["change"]
  return {
    path: d.path,
    change: change === "changed" || change === "unchanged" ? change : "new",
    from: change === "changed" ? d.version - 1 : undefined,
    to: d.version,
    view: {
      id: documentViewId(d.path, d.version), kind: kindForMedia(d.media_type ?? undefined, d.path), title: d.path, source: "input",
      version: `v${d.version}`, versions: versions.map((v) => `v${v}`), docKey: d.doc_key, mediaType: d.media_type ?? undefined,
    },
  }
}

/**
 * Session commands over the HTTP API. One `Command` (one command ID) per call, so a click's
 * automatic retries reuse its ID. The receipt confirms the commit; the stream confirms the effect.
 */
export function sessionCommands(ctx: CommandContext): SessionCommands {
  const now = ctx.now ?? (() => new Date().toISOString())
  // The open run the server named when it refused a message with lane_required.
  let refusedFor: string | undefined

  const submitting = async (interruptId: string, f: () => Promise<unknown>) => {
    ctx.dispatch({ type: "submitting", interruptId, on: true })
    try { await f() } finally { ctx.dispatch({ type: "submitting", interruptId, on: false }) }
  }

  return {
    async send({ text, lane, agent, files }: SendInput) {
      const s = ctx.getState()
      const run = openRun(s)
      const runId = run && (run.status === "run" || run.status === "wait") ? run.id : refusedFor
      if (lane && runId) {
        const cmd = endpoints.commands.steer(runId, { text, lane })
        ctx.dispatch({ type: "optimistic-message", commandId: cmd.id, text, lane, files, at: now() })
        try { await cmd.send() } catch (e) {
          ctx.dispatch({ type: "command-refused", commandId: cmd.id })
          if (isApiError(e) && e.code === "run_ended") refusedFor = undefined
          throw e
        }
        refusedFor = undefined
        return
      }
      const session = s.sessionId ?? newSessionId()
      const cmd = endpoints.commands.sendMessage(session, { text, agent })
      const seed: LocalAction = { type: "optimistic-message", commandId: cmd.id, text, files, agent, at: now() }
      if (s.sessionId) ctx.dispatch(seed)
      try {
        await cmd.send()
      } catch (e) {
        if (s.sessionId) ctx.dispatch({ type: "command-refused", commandId: cmd.id })
        if (isApiError(e) && e.code === "lane_required" && s.sessionId) {
          // Our view of the run is behind: ask the server which run is open, then the composer asks for a lane.
          try { refusedFor = (await endpoints.reads.session(s.sessionId)).run?.run ?? undefined } catch { refusedFor = undefined }
        }
        throw e
      }
      if (!s.sessionId) ctx.onNewSession?.(session, seed)
    },

    async act(interruptId: string, a: InterruptAction) {
      const i = ctx.getState().interrupts[interruptId]
      switch (a.kind) {
        case "answer":
          return submitting(interruptId, () => endpoints.commands.answer(interruptId, { text: a.text, answers: a.answers, agent: a.agent }).send())
        case "approve":
          // Always the full signature the card showed: the server compares it byte for byte.
          return submitting(interruptId, () => endpoints.commands.approve(interruptId, a.signature).send())
        case "reject":
          return submitting(interruptId, () => endpoints.commands.reject(interruptId, a.note).send())
        case "grant": {
          const dim = i?.budget?.dimension ?? "model_turns"
          const extra = Math.max(1, Math.ceil((i?.budget?.allowed ?? 20) / 2))
          return submitting(interruptId, () => endpoints.commands.grantBudget(interruptId, { [dim]: extra }).send())
        }
        case "finalize":
          return submitting(interruptId, () => endpoints.commands.finalize(interruptId).send())
        case "stop":
          if (!i?.runId) return notYet("Stopping from this card")
          return submitting(interruptId, () => endpoints.commands.stop(i.runId!).send())
        case "accept-review": {
          const hash = i?.review?.cardHash
          if (!hash) return notYet("Accepting a review without its card hash")
          return submitting(interruptId, () => endpoints.commands.acceptReview(interruptId, hash).send())
        }
        case "reject-review":
          return submitting(interruptId, () => endpoints.commands.rejectReview(interruptId, i?.review?.cardHash).send())
        default:
          return notYet(`The ${a.kind} action`)
      }
    },

    async stop(runId: string) { await endpoints.commands.stop(runId).send() },

    async upload(files: File[], onProgress?: (p: UploadProgress[]) => void) {
      const parts = files.map((f) => ({ file: f, path: uploadPath(f) }))
      const sizes = parts.map((p) => p.file.size)
      const sum = sizes.reduce((a, b) => a + b, 0) || 1
      const report = (loaded: number, total: number) => {
        // Parts go in order, so body bytes map onto files by their sizes.
        let left = (loaded / (total || 1)) * sum
        onProgress?.(parts.map((p, n) => {
          const got = Math.max(0, Math.min(sizes[n], left))
          left -= sizes[n]
          return { path: p.path, loaded: got, total: sizes[n] }
        }))
      }
      const res = await endpoints.uploadInputs(parts, report)
      report(1, 1)
      const stored = res.documents.length
        ? res.documents.map(documentUpload)
        : parts.map((p) => documentUpload({ doc_key: "", path: p.path, version: 1, versions: [], media_type: p.file.type || null }))
      ctx.dispatch({ type: "uploaded", files: stored.map((f) => f.view) })
      return stored
    },
  }
}
