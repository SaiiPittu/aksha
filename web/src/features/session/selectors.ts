import type { Part, Run, SessionState, ViewRef } from "./types"
import { artifactView } from "./reducer"

/** The run the header shows: the last one still open, else the last one. */
export function openRun(s: SessionState): Run | undefined {
  for (let i = s.runs.length - 1; i >= 0; i--) {
    const r = s.runs[i]
    if (r.status === "run" || r.status === "wait") return r
  }
  return s.runs.at(-1)
}

function viewsInPart(p: Part): ViewRef[] {
  switch (p.type) {
    case "artifact": return [p.view]
    case "files": return p.files
    case "tool": return p.view ? [p.view] : []
    case "step-group": return [...(p.saved ?? []), ...p.steps.flatMap(viewsInPart)]
    case "text": return (p.citations ?? []).map((c) => c.view)
    default: return []
  }
}

/** Resolves a `?view=` ID against everything the session can open. */
export function findView(s: SessionState, id: string): ViewRef | undefined {
  const a = s.artifacts.find((v) => v.id === id)
  if (a) return a
  for (const m of s.messages) for (const p of m.parts) {
    const v = viewsInPart(p).find((x) => x.id === id)
    if (v) return v
  }
  for (const i of Object.values(s.interrupts)) {
    if (i.action?.payloadView?.id === id) return i.action.payloadView
    if (i.review?.view?.id === id) return i.review.view
  }
  // A link to an artifact version this session has not shown (an older version, a shared link).
  if (id.startsWith("artifact:") && id.includes("@")) return artifactView(id.slice("artifact:".length))
  return undefined
}
