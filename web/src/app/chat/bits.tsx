import {
  Activity, Braces, Code2, FileCode2, FileText, Frame, Globe, GitCompareArrows, Image, LayoutDashboard, Puzzle, Sheet, FileType2,
} from "lucide-react"
import { cn } from "@/lib/utils"
import type { Status, ViewKind, ViewRef } from "@/features/session/types"
import { statusGlyph } from "@/app/shell/Status"

/** Handler every chip, citation and card uses to open a view in the panel. */
export type OpenView = (v: ViewRef) => void

/** Marks an element that opens a view, so a click on it never counts as "click outside" for a peek panel. */
export const opensView = { "data-opens-view": "" } as const

export function ViewIcon({ kind, className }: { kind: ViewKind; className?: string }) {
  const c = cn("size-4", className)
  switch (kind) {
    case "pdf": return <FileText className={c} />
    case "markdown": return <FileType2 className={c} />
    case "table": return <Sheet className={c} />
    case "diff": return <GitCompareArrows className={c} />
    case "web": return <Globe className={c} />
    case "embed": return <Frame className={c} />
    case "html": return <Code2 className={c} />
    case "ui-spec": return <LayoutDashboard className={c} />
    case "trace": return <Activity className={c} />
    case "image": return <Image className={c} />
    case "text": return <FileCode2 className={c} />
    case "json": return <Braces className={c} />
    default: return <Puzzle className={c} />
  }
}

export const kindLabel = (k: ViewKind) =>
  ({ pdf: "PDF", json: "JSON", markdown: "Markdown", table: "Table", diff: "Diff", web: "Web page", embed: "Embed", html: "HTML page",
     "ui-spec": "View", trace: "Trace", image: "Image", text: "Text" } as Record<string, string>)[k] ?? k.replace("plugin:", "Plugin · ")

export const fmtMs = (ms?: number) => ms == null ? "" : ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`

const glyphTone: Record<Status, string> = {
  wait: "text-status-wait", run: "text-status-run", ok: "text-status-ok", bad: "text-status-bad", idle: "text-muted-foreground",
}

/** A status glyph sized for inline rows (✓ ◆ ● ✗ ○). */
export function Glyph({ status, className }: { status: Status; className?: string }) {
  return <span aria-hidden className={cn("inline-grid w-3.5 shrink-0 place-items-center text-[11px] leading-none", glyphTone[status], className)}>{statusGlyph[status]}</span>
}

/** "▲ leaves Aksha": the red marker for any action whose effect goes outside the system. */
export function LeavesBadge({ className }: { className?: string }) {
  return (
    <span className={cn("inline-flex h-5 items-center gap-1 rounded-full bg-status-bad-soft px-2 text-[11.5px] font-medium whitespace-nowrap text-status-bad", className)}>
      <span aria-hidden className="text-[9px]">▲</span>leaves Aksha
    </span>
  )
}

export function AgentMark({ name, className }: { name: string; className?: string }) {
  return (
    <span className={cn("grid size-6 shrink-0 place-items-center rounded-md border bg-muted text-[11px] font-semibold text-foreground", className)}>
      {name.slice(0, 1).toUpperCase()}
    </span>
  )
}
