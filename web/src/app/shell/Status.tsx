import { cn } from "@/lib/utils"
import type { Status } from "@/features/session/types"

// The one status vocabulary: colour plus a glyph, never colour alone. The TUI uses the same glyphs.
export const statusGlyph: Record<Status, string> = { wait: "◆", run: "●", ok: "✓", bad: "✗", idle: "○" }

const tone: Record<Status, string> = {
  wait: "bg-status-wait-soft text-status-wait",
  run: "bg-status-run-soft text-status-run",
  ok: "bg-status-ok-soft text-status-ok",
  bad: "bg-status-bad-soft text-status-bad",
  idle: "bg-muted text-muted-foreground",
}

export function StatusChip({ status, children, className }: { status: Status; children: React.ReactNode; className?: string }) {
  return (
    <span className={cn("inline-flex h-5 items-center gap-1 rounded-full px-2 text-[11.5px] font-medium whitespace-nowrap", tone[status], className)}>
      <span aria-hidden className={cn("text-[9px] leading-none", status === "run" && "animate-pulse")}>{statusGlyph[status]}</span>
      {children}
    </span>
  )
}

export function StatusDot({ status }: { status: Status }) {
  return <span aria-hidden className={cn("text-[10px]", tone[status].split(" ")[1])}>{statusGlyph[status]}</span>
}
