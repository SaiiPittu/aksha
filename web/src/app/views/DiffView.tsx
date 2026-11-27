import { useMemo, useState } from "react"
import { cn } from "@/lib/utils"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"

type Op = { t: "same" | "del" | "add"; text: string; a?: number; b?: number }

/** Line diff by longest common subsequence (fine for documents of a few thousand lines). */
export function lineDiff(before: string, after: string): Op[] {
  const A = before.split("\n"), B = after.split("\n")
  const n = A.length, m = B.length
  const L = Array.from({ length: n + 1 }, () => new Array<number>(m + 1).fill(0))
  for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--)
    L[i][j] = A[i] === B[j] ? L[i + 1][j + 1] + 1 : Math.max(L[i + 1][j], L[i][j + 1])
  const out: Op[] = []
  let i = 0, j = 0
  while (i < n || j < m) {
    if (i < n && j < m && A[i] === B[j]) { out.push({ t: "same", text: A[i], a: i + 1, b: j + 1 }); i++; j++ }
    else if (i < n && (j >= m || L[i + 1][j] >= L[i][j + 1])) { out.push({ t: "del", text: A[i], a: i + 1 }); i++ }
    else { out.push({ t: "add", text: B[j], b: j + 1 }); j++ }
  }
  return out
}

function parseCsv(s: string) {
  return s.split("\n").map((line) => {
    const cells: string[] = []; let cur = "", q = false
    for (const ch of line) {
      if (ch === '"') q = !q
      else if (ch === "," && !q) { cells.push(cur); cur = "" }
      else cur += ch
    }
    cells.push(cur); return cells
  })
}

export function DiffView({ before, after, label, fromLabel = "v1", toLabel = "v2", tableable }: {
  before: string; after: string; label: string; fromLabel?: string; toLabel?: string; tableable?: boolean
}) {
  const [mode, setMode] = useState<"text" | "table">("text")
  const ops = useMemo(() => lineDiff(before, after), [before, after])
  const adds = ops.filter((o) => o.t === "add").length, dels = ops.filter((o) => o.t === "del").length
  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex h-10 shrink-0 items-center gap-3 border-b px-4 text-[13px]">
        <span className="font-mono text-[12.5px]">{label}</span>
        <span className="text-muted-foreground">{fromLabel} → {toLabel}</span>
        <span className="flex items-center gap-2 font-mono text-[12px] tabular-nums">
          <span className="text-status-ok">+{adds}</span><span className="text-status-bad">−{dels}</span>
        </span>
        {tableable && (
          <ToggleGroup type="single" size="sm" variant="outline" value={mode} onValueChange={(v) => v && setMode(v as "text" | "table")} className="ml-auto">
            <ToggleGroupItem value="text" className="h-6! px-2! text-xs!">Lines</ToggleGroupItem>
            <ToggleGroupItem value="table" className="h-6! px-2! text-xs!">Cells</ToggleGroupItem>
          </ToggleGroup>
        )}
      </div>
      <div className="min-h-0 flex-1 overflow-auto">
        {mode === "text" ? <UnifiedDiff ops={ops} /> : <CellDiff before={before} after={after} />}
      </div>
    </div>
  )
}

function UnifiedDiff({ ops }: { ops: Op[] }) {
  return (
    <table className="w-full border-collapse font-mono text-[12.5px] leading-[22px]">
      <tbody>
        {ops.map((o, k) => (
          <tr key={k} className={cn(o.t === "add" && "bg-status-ok-soft", o.t === "del" && "bg-status-bad-soft")}>
            <td className="w-10 border-r px-2 text-right text-[11px] text-muted-foreground/70 tabular-nums select-none">{o.a ?? ""}</td>
            <td className="w-10 border-r px-2 text-right text-[11px] text-muted-foreground/70 tabular-nums select-none">{o.b ?? ""}</td>
            <td className={cn("w-6 text-center select-none", o.t === "add" ? "text-status-ok" : o.t === "del" ? "text-status-bad" : "text-muted-foreground/50")}>
              {o.t === "add" ? "+" : o.t === "del" ? "−" : ""}
            </td>
            <td className={cn("pr-4 whitespace-pre", o.t === "del" && "text-status-bad", o.t === "add" && "text-status-ok")}>{o.text || " "}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

/** Field-level redline: rows matched by their first cell, changed cells show old struck through and new. */
function CellDiff({ before, after }: { before: string; after: string }) {
  const [hb, ...rb] = parseCsv(before), [ha, ...ra] = parseCsv(after)
  const cols = ha.length >= hb.length ? ha : hb
  const old = new Map(rb.map((r) => [r[0], r]))
  const rows = ra.map((r) => ({ r, o: old.get(r[0]), state: old.has(r[0]) ? "keep" : "add" }))
  const removed = rb.filter((r) => !ra.some((x) => x[0] === r[0]))
  return (
    <table className="w-full border-collapse text-[13px]">
      <thead className="sticky top-0 bg-muted/80 backdrop-blur">
        <tr>{["", ...cols].map((c, i) => <th key={i} className="border-b px-3 py-2 text-left font-mono text-[11.5px] font-medium text-muted-foreground">{c}</th>)}</tr>
      </thead>
      <tbody>
        {rows.map(({ r, o, state }) => {
          const changed = o && r.some((c, i) => c !== o[i])
          return (
            <tr key={r[0]} className={cn("border-b", state === "add" && "bg-status-ok-soft")}>
              <td className={cn("w-6 px-2 text-center font-mono", state === "add" ? "text-status-ok" : changed ? "text-status-wait" : "text-muted-foreground/40")}>
                {state === "add" ? "+" : changed ? "~" : ""}
              </td>
              {r.map((c, i) => {
                const was = o?.[i]
                return (
                  <td key={i} className="px-3 py-1.5 tabular-nums">
                    {o && was !== c ? (
                      <span className="inline-flex flex-wrap items-baseline gap-1.5">
                        <s className="rounded-sm bg-status-bad-soft px-1 text-status-bad">{was}</s>
                        <span className="rounded-sm bg-status-ok-soft px-1 text-status-ok">{c}</span>
                      </span>
                    ) : c}
                  </td>
                )
              })}
            </tr>
          )
        })}
        {removed.map((r) => (
          <tr key={r[0]} className="border-b bg-status-bad-soft text-status-bad">
            <td className="w-6 px-2 text-center font-mono">−</td>
            {r.map((c, i) => <td key={i} className="px-3 py-1.5 line-through">{c}</td>)}
          </tr>
        ))}
      </tbody>
    </table>
  )
}
