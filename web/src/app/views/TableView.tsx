import { useMemo, useState } from "react"
import { ArrowDown, ArrowUp, ArrowUpDown, Filter, Info } from "lucide-react"
import { Input } from "@/components/ui/input"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { cn } from "@/lib/utils"

type Cell = string | number
const num = (c: Cell) => (typeof c === "number" ? c : /^[€$£]?\s?[\d,]+(\.\d+)?$/.test(c) ? Number(c.replace(/[^\d.]/g, "")) : NaN)

/** A dense, sortable, filterable grid with a sticky header and per-row provenance on hover. */
export function TableView({ columns, rows, provenance }: { columns: string[]; rows: Cell[][]; provenance?: Record<string, string> }) {
  const [sort, setSort] = useState<{ col: number; dir: 1 | -1 } | null>(null)
  const [q, setQ] = useState("")
  const [sel, setSel] = useState<number | null>(null)
  const numeric = columns.map((_, i) => rows.every((r) => !Number.isNaN(num(r[i]))))
  const sourceCol = columns.findIndex((c) => /^(source|from|ref)$/i.test(c))

  const view = useMemo(() => {
    let out = rows.map((r, i) => ({ r, i }))
    if (q.trim()) out = out.filter(({ r }) => r.some((c) => String(c).toLowerCase().includes(q.toLowerCase())))
    if (sort) out = [...out].sort((a, b) => {
      const x = a.r[sort.col], y = b.r[sort.col]
      const nx = num(x), ny = num(y)
      return (!Number.isNaN(nx) && !Number.isNaN(ny) ? nx - ny : String(x).localeCompare(String(y))) * sort.dir
    })
    return out
  }, [rows, q, sort])

  const provFor = (r: Cell[], i: number) =>
    provenance?.[String(r[0])] ?? (sourceCol >= 0 ? `extracted from ${r[sourceCol]}` : `row ${i + 1} of the source`)

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex h-10 shrink-0 items-center gap-2 border-b px-3">
        <div className="relative w-56">
          <Filter className="pointer-events-none absolute top-1/2 left-2 size-3.5 -translate-y-1/2 text-muted-foreground" />
          <Input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Filter rows" className="h-7! pl-7! text-[12.5px]! md:text-[12.5px]!" />
        </div>
        <span className="text-[12px] text-muted-foreground tabular-nums">{view.length} of {rows.length} rows · {columns.length} columns</span>
        <span className="ml-auto hidden items-center gap-1 text-[11.5px] text-muted-foreground @[560px]:inline-flex"><Info className="size-3" />Hover a row for its source</span>
      </div>
      <div className="min-h-0 flex-1 overflow-auto">
        <table className="w-full border-separate border-spacing-0 text-[12.5px]">
          <thead className="sticky top-0 z-10 bg-muted">
            <tr>
              <th className="w-9 border-b px-2 py-2 text-right font-mono text-[11px] font-normal text-muted-foreground">#</th>
              {columns.map((c, i) => {
                const active = sort?.col === i
                const Icon = !active ? ArrowUpDown : sort.dir === 1 ? ArrowUp : ArrowDown
                return (
                  <th key={c} className={cn("border-b border-l px-2 py-0 font-medium whitespace-nowrap", numeric[i] ? "text-right" : "text-left")}>
                    <button
                      onClick={() => setSort(active ? (sort.dir === 1 ? { col: i, dir: -1 } : null) : { col: i, dir: 1 })}
                      className={cn("group inline-flex h-8 items-center gap-1.5 font-mono text-[11.5px] hover:text-foreground", active ? "text-foreground" : "text-muted-foreground", numeric[i] && "flex-row-reverse")}
                    >
                      {c}<Icon className={cn("size-3", !active && "opacity-0 group-hover:opacity-60")} />
                    </button>
                  </th>
                )
              })}
            </tr>
          </thead>
          <tbody>
            {view.map(({ r, i }) => (
              <Tooltip key={i}>
                <TooltipTrigger asChild>
                  <tr onClick={() => setSel(i)} className={cn("cursor-default hover:bg-accent/60", sel === i && "bg-accent")}>
                    <td className="border-b px-2 py-1.5 text-right align-top font-mono text-[11px] text-muted-foreground tabular-nums">{i + 1}</td>
                    {r.map((c, j) => (
                      <td key={j} className={cn("border-b border-l px-2 py-1.5 align-top", String(c).length < 17 && "whitespace-nowrap", numeric[j] && "text-right tabular-nums", j === 0 && "font-medium", j === sourceCol && "font-mono text-[11.5px] text-muted-foreground")}>
                        {typeof c === "number" ? c.toLocaleString("en-GB") : c}
                      </td>
                    ))}
                  </tr>
                </TooltipTrigger>
                <TooltipContent side="left" align="center" className="max-w-xs">
                  <span className="opacity-70">Provenance</span> {provFor(r, i)}
                </TooltipContent>
              </Tooltip>
            ))}
            {view.length === 0 && (
              <tr><td colSpan={columns.length + 1} className="px-3 py-10 text-center text-muted-foreground">No rows match “{q}”.</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  )
}
