import { useMemo, useState } from "react"
import { ChevronDown, ChevronRight } from "lucide-react"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { cn } from "@/lib/utils"
import { statusGlyph } from "@/app/shell/Status"

// A run trace: a span tree on the left, a waterfall on the right, details for the selected span.
// The spans come from the run's span projection (ViewRef.spans).

import type { TraceSpan as Span, TraceSpanType as SpanType } from "@/features/session/types"

const typeBadge: Record<SpanType, string> = {
  turn: "bg-muted text-muted-foreground",
  model: "bg-secondary text-secondary-foreground ring-1 ring-border",
  tool: "bg-background text-foreground ring-1 ring-border",
  interrupt: "bg-background text-muted-foreground ring-1 ring-border",
}
const bar = (s: Span) =>
  s.status === "bad" ? "bg-status-bad" : s.status === "wait" ? "bg-status-wait" :
  s.type === "turn" ? "bg-foreground/15" : s.type === "model" ? "bg-primary/75" : s.type === "interrupt" ? "bg-muted-foreground/40" : "bg-muted-foreground/60"

const fmt = (ms: number) => (ms < 1000 ? `${ms}ms` : ms < 60000 ? `${(ms / 1000).toFixed(1)}s` : `${Math.floor(ms / 60000)}m ${Math.round((ms % 60000) / 1000)}s`)

export function TraceView({ spans }: { spans: Span[] }) {
  if (!spans.length) return <div className="grid h-full place-items-center text-[13px] text-muted-foreground">No spans recorded for this run yet.</div>
  return <TraceTree spans={spans} />
}

function TraceTree({ spans }: { spans: Span[] }) {
  const [open, setOpen] = useState<Record<string, boolean>>({})
  const [sel, setSel] = useState(spans[0]?.id ?? "")
  const total = Math.max(...spans.map((s) => s.start + s.dur))
  const rows = useMemo(() => {
    const out: (Span & { depth: number; kids: number })[] = []
    const walk = (p: string | undefined, d: number) => spans.filter((s) => s.parent === p).forEach((s) => {
      const kids = spans.filter((k) => k.parent === s.id).length
      out.push({ ...s, depth: d, kids })
      if (open[s.id] !== false) walk(s.id, d + 1)
    })
    walk(undefined, 0)
    return out
  }, [spans, open])
  const cur = spans.find((s) => s.id === sel)
  // The waterfall window: the selected span's turn (so short parallel calls are readable), or the whole run.
  const [scope, setScope] = useState<"turn" | "run">("turn")
  const root = (s?: Span): Span | undefined => (s?.parent ? root(spans.find((x) => x.id === s.parent)) : s)
  const rt = root(cur)
  const w0 = scope === "turn" && rt ? rt.start : 0
  const w1 = scope === "turn" && rt ? rt.start + rt.dur : total
  const span = w1 - w0
  const ticks = [0, 1 / 3, 2 / 3, 1]
  const pct = (ms: number) => `${(ms / span) * 100}%`

  return (
    <div className="flex h-full min-h-0 flex-col text-[12.5px]">
      <div className="flex h-10 shrink-0 items-center gap-3 border-b px-4 whitespace-nowrap text-muted-foreground">
        <span className="inline-flex h-5 items-center gap-1 rounded-full bg-status-wait-soft px-2 text-[11.5px] text-status-wait"><span className="text-[9px]">{statusGlyph.wait}</span>waiting</span>
        {[["elapsed", fmt(total)], ["turns", "9"], ["tokens", "21.4k"], ["spans", String(spans.length)]].map(([k, v], i) => (
          <span key={k} className={cn(i > 1 && "hidden @[560px]:inline")}><span className="font-medium text-foreground tabular-nums">{v}</span> {k}</span>
        ))}
        <ToggleGroup type="single" size="sm" variant="outline" value={scope} onValueChange={(v) => v && setScope(v as "turn" | "run")} className="ml-auto">
          <ToggleGroupItem value="turn" className="h-6! px-2! text-[11.5px]!">This turn</ToggleGroupItem>
          <ToggleGroupItem value="run" className="h-6! px-2! text-[11.5px]!">Whole run</ToggleGroupItem>
        </ToggleGroup>
      </div>
      <div className="grid shrink-0 grid-cols-[minmax(200px,52%)_1fr] border-b bg-muted/50 text-[11px] text-muted-foreground">
        <div className="flex h-7 items-center justify-between border-r px-3"><span>Span</span><span>Duration</span></div>
        <div className="relative h-7">
          {ticks.map((t) => (
            <span key={t} className={cn("absolute top-1/2 -translate-y-1/2 font-mono tabular-nums", t === 1 ? "right-2" : t === 0 ? "left-2" : "-translate-x-1/2")} style={t > 0 && t < 1 ? { left: `${t * 100}%` } : undefined}>
              {fmt(Math.round(w0 + span * t))}
            </span>
          ))}
        </div>
      </div>
      <div className="min-h-0 flex-1 overflow-auto">
        {rows.map((s) => (
          <div key={s.id} onClick={() => setSel(s.id)}
            className={cn("grid cursor-default grid-cols-[minmax(200px,52%)_1fr] border-b border-border/60", sel === s.id ? "bg-accent" : "hover:bg-accent/50")}>
            <div className="flex h-7 min-w-0 items-center gap-1.5 border-r pr-3" style={{ paddingLeft: 8 + s.depth * 16 }}>
              {s.kids ? (
                <button aria-label="Toggle children" className="shrink-0 text-muted-foreground hover:text-foreground" onClick={(e) => { e.stopPropagation(); setOpen((o) => ({ ...o, [s.id]: o[s.id] === false })) }}>
                  {open[s.id] === false ? <ChevronRight className="size-3.5" /> : <ChevronDown className="size-3.5" />}
                </button>
              ) : <span className="w-3.5 shrink-0" />}
              <span className={cn("shrink-0 rounded px-1 py-px font-mono text-[10px] leading-4", typeBadge[s.type])}>{s.type}</span>
              <span className={cn("min-w-0 truncate", s.type === "turn" && "font-medium")}>{s.name}</span>
              {s.status === "bad" && <span className="text-status-bad">✗</span>}
              {s.status === "wait" && <span className="text-[9px] text-status-wait">◆</span>}
              <span className="ml-auto shrink-0 pl-2 font-mono text-[11px] text-muted-foreground tabular-nums">{fmt(s.dur)}</span>
            </div>
            <div className="relative h-7 overflow-hidden">
              {ticks.slice(1, -1).map((t) => <span key={t} className="absolute inset-y-0 w-px bg-border/70" style={{ left: `${t * 100}%` }} />)}
              <span className={cn("absolute top-1/2 h-3 -translate-y-1/2 rounded-[3px]", bar(s), s.type === "turn" && "h-1.5")}
                style={{ left: pct(s.start - w0), width: `max(3px, ${pct(s.dur)})` }} />
            </div>
          </div>
        ))}
      </div>
      <div className="flex h-7 shrink-0 items-center gap-3 border-t px-4 text-[11px] text-muted-foreground">
        <Legend c="bg-primary/75" l="model call" /><Legend c="bg-muted-foreground/60" l="tool call" /><Legend c="bg-muted-foreground/40" l="interrupt" />
        <Legend c="bg-status-bad" l="✗ refused" /><Legend c="bg-status-wait" l="◆ waiting on a person" />
      </div>
      {cur && (
        <div className="h-[32%] min-h-40 shrink-0 overflow-auto border-t bg-muted/30">
          <div className="flex h-9 items-center gap-2 border-b px-4">
            <span className={cn("rounded px-1 py-px font-mono text-[10px] leading-4", typeBadge[cur.type])}>{cur.type}</span>
            <span className="font-medium">{cur.name}</span>
            <span className={cn("ml-1 text-[11.5px]", cur.status === "bad" ? "text-status-bad" : cur.status === "wait" ? "text-status-wait" : "text-status-ok")}>
              {statusGlyph[cur.status ?? "ok"]} {cur.status === "bad" ? "refused" : cur.status === "wait" ? "waiting" : "ok"}
            </span>
            <span className="ml-auto font-mono text-[11px] text-muted-foreground">start +{fmt(cur.start)} · {fmt(cur.dur)}</span>
          </div>
          <dl className="grid grid-cols-[140px_1fr] gap-x-4 gap-y-1.5 px-4 py-3">
            {[["span id", `sp_${cur.id}`], ...(cur.parent ? [["parent", `sp_${cur.parent}`]] : []), ...cur.attrs].map(([k, v]) => (
              <div key={k} className="contents">
                <dt className="text-muted-foreground">{k}</dt>
                <dd className={cn("font-mono text-[12px]", k === "refusal" && "text-status-bad")}>{v}</dd>
              </div>
            ))}
          </dl>
        </div>
      )}
    </div>
  )
}

const Legend = ({ c, l }: { c: string; l: string }) => <span className="inline-flex items-center gap-1"><span className={cn("h-2 w-3 rounded-sm", c)} />{l}</span>
