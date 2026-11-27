import { useState } from "react"
import { ChevronRight } from "lucide-react"
import { cn } from "@/lib/utils"

/** A collapsible JSON tree: objects and arrays fold; the first two levels start open. */
export function JsonView({ value }: { value: unknown }) {
  const [raw, setRaw] = useState(false)
  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex h-9 shrink-0 items-center gap-2 border-b px-3 text-[12px] text-muted-foreground">
        <span>JSON</span>
        <button onClick={() => setRaw(!raw)} className="ml-auto rounded px-1.5 py-0.5 hover:bg-muted hover:text-foreground">{raw ? "Tree" : "Raw"}</button>
      </div>
      <div className="min-h-0 flex-1 overflow-auto p-3 font-mono text-[12.5px] leading-5">
        {raw ? <pre className="whitespace-pre-wrap break-words">{JSON.stringify(value, null, 2)}</pre> : <Node k={undefined} v={value} depth={0} />}
      </div>
    </div>
  )
}

function Scalar({ v }: { v: unknown }) {
  if (v === null) return <span className="text-muted-foreground">null</span>
  if (typeof v === "string") return <span className="break-words text-status-ok">"{v}"</span>
  if (typeof v === "number") return <span className="text-status-run">{v}</span>
  if (typeof v === "boolean") return <span className="text-status-wait">{String(v)}</span>
  return <span>{String(v)}</span>
}

function Node({ k, v, depth }: { k?: string; v: unknown; depth: number }) {
  const branch = v !== null && typeof v === "object"
  const [open, setOpen] = useState(depth < 2)
  const key = k !== undefined && <span className="text-foreground">{k}<span className="text-muted-foreground">: </span></span>
  if (!branch) return <div className="pl-4">{key}<Scalar v={v} /></div>
  const entries = Array.isArray(v) ? v.map((x, i) => [String(i), x] as const) : Object.entries(v as Record<string, unknown>)
  const brackets = Array.isArray(v) ? ["[", "]"] : ["{", "}"]
  return (
    <div>
      <button onClick={() => setOpen(!open)} aria-expanded={open} className="group flex items-center text-left hover:bg-muted/60">
        <ChevronRight className={cn("size-3.5 shrink-0 text-muted-foreground transition-transform", open && "rotate-90")} />
        {key}<span className="text-muted-foreground">{brackets[0]}{!open && <> {entries.length} {Array.isArray(v) ? "items" : "keys"} {brackets[1]}</>}</span>
      </button>
      {open && <div className="ml-[7px] border-l pl-1">{entries.map(([ek, ev]) => <Node key={ek} k={ek} v={ev} depth={depth + 1} />)}</div>}
      {open && <div className="pl-4 text-muted-foreground">{brackets[1]}</div>}
    </div>
  )
}

