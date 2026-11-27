import type { UiSpecNode } from "@/features/session/types"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { StatusChip, statusGlyph } from "@/app/shell/Status"
import type { Status } from "@/features/session/types"
import { cn } from "@/lib/utils"

// Renders a declarative ui-spec tree with the app's own components, so generative UI stays on-theme
// and safe. Used inline in the thread and in the panel. Unknown node types render nothing.

const str = (v: unknown, d = "") => (v == null ? d : String(v))
const tone = (v: unknown): Status | undefined => (["wait", "run", "ok", "bad", "idle"].includes(String(v)) ? (v as Status) : undefined)
const toneText: Record<Status, string> = { wait: "text-status-wait", run: "text-status-run", ok: "text-status-ok", bad: "text-status-bad", idle: "text-muted-foreground" }

export function UiSpec({ spec, className }: { spec: UiSpecNode; className?: string }) {
  return <div className={cn("@container", className)}><Node n={spec} /></div>
}

function Kids({ n }: { n: UiSpecNode }) {
  return <>{n.children?.map((c, i) => <Node key={i} n={c} />)}</>
}

function Node({ n }: { n: UiSpecNode }) {
  const p = n.props ?? {}
  switch (n.type) {
    case "stack":
      return p.direction === "column"
        ? <div className="flex flex-col gap-3"><Kids n={n} /></div>
        : <div className="grid grid-cols-1 gap-2.5 @sm:grid-cols-[repeat(auto-fit,minmax(150px,1fr))]"><Kids n={n} /></div>
    case "card":
      return (
        <Card size="sm" className="gap-3 py-4">
          {(p.title != null || p.description != null) && (
            <CardHeader className="px-4">
              {p.title != null && <CardTitle className="text-[14px]">{str(p.title)}</CardTitle>}
              {p.description != null && <CardDescription className="text-[12.5px]">{str(p.description)}</CardDescription>}
            </CardHeader>
          )}
          <CardContent className="flex flex-col gap-3 px-4"><Kids n={n} /></CardContent>
        </Card>
      )
    case "text":
      return <p className={cn("text-[13.5px] leading-relaxed", p.muted ? "text-muted-foreground" : "text-foreground")}>{str(p.text ?? p.value)}</p>
    case "metric": {
      const t = tone(p.tone)
      return (
        <div className="rounded-lg border bg-card px-3.5 py-3">
          <div className="text-[12px] text-muted-foreground">{str(p.label)}</div>
          <div className={cn("mt-1 flex items-baseline gap-1.5 text-[22px] leading-tight font-semibold tracking-tight tabular-nums", t && toneText[t])}>
            {t && t !== "idle" && <span aria-hidden className="text-[12px]">{statusGlyph[t]}</span>}
            {str(p.value)}
          </div>
          {p.hint != null && <div className="mt-0.5 text-[11.5px] text-muted-foreground">{str(p.hint)}</div>}
        </div>
      )
    }
    case "badge": {
      const t = tone(p.tone)
      return t ? <StatusChip status={t}>{str(p.label)}</StatusChip> : <Badge variant="secondary">{str(p.label)}</Badge>
    }
    case "button":
      return (
        <div><Button size="sm" variant={p.variant === "primary" ? "default" : "outline"}>{str(p.label, "Action")}</Button></div>
      )
    case "table": {
      const cols = (p.columns as string[] | undefined) ?? []
      const rows = (p.rows as (string | number)[][] | undefined) ?? []
      return (
        <div className="overflow-hidden rounded-lg border">
          <table className="w-full text-[13px]">
            <thead className="bg-muted/60"><tr>{cols.map((c) => <th key={c} className="border-b px-3 py-1.5 text-left text-[12px] font-medium text-muted-foreground">{c}</th>)}</tr></thead>
            <tbody>{rows.map((r, i) => <tr key={i} className="border-b last:border-0">{r.map((c, j) => <td key={j} className="px-3 py-1.5 tabular-nums">{c}</td>)}</tr>)}</tbody>
          </table>
        </div>
      )
    }
    case "chart-bar": {
      const items = (p.items as { label: string; value: number; note?: string }[] | undefined) ?? []
      const max = Math.max(1, ...items.map((i) => i.value))
      return (
        <div className="flex flex-col gap-2">
          {p.title != null && <div className="text-[12.5px] font-medium">{str(p.title)}</div>}
          {items.map((it) => (
            <div key={it.label} className="grid grid-cols-[120px_1fr_auto] items-center gap-3 text-[12.5px]">
              <span className="truncate text-muted-foreground">{it.label}</span>
              <span className="h-2.5 rounded-sm bg-muted"><span className="block h-full rounded-sm bg-primary/80" style={{ width: `${(it.value / max) * 100}%` }} /></span>
              <span className="tabular-nums">{it.note ?? it.value}</span>
            </div>
          ))}
        </div>
      )
    }
    default:
      return null
  }
}
