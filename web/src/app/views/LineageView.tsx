import { ArrowDown, Database, FileText, Package, Search } from "lucide-react"
import { useLineage, type Lineage } from "@/api"
import { Spinner } from "@/components/ui/spinner"
import { cn } from "@/lib/utils"

type Node = Lineage["nodes"][number]

const freshTone: Record<string, string> = {
  current: "bg-status-ok-soft text-status-ok",
  current_by_cutoff: "bg-status-ok-soft text-status-ok",
  dirty: "bg-status-wait-soft text-status-wait",
  invalidated: "bg-status-bad-soft text-status-bad",
}
const freshLabel: Record<string, string> = { current: "current", current_by_cutoff: "current (cutoff)", dirty: "rebuilding", invalidated: "stale" }

export function FreshnessChip({ f }: { f?: string | null }) {
  if (!f) return null
  return <span className={cn("inline-flex h-[18px] shrink-0 items-center rounded-full px-1.5 text-[10.5px] font-medium", freshTone[f] ?? "bg-muted text-muted-foreground")}>{freshLabel[f] ?? f}</span>
}

function NodeIcon({ type }: { type: string }) {
  const c = "size-3.5 shrink-0 text-muted-foreground"
  if (type === "document" || type === "record") return <FileText className={c} />
  if (type === "query") return <Search className={c} />
  if (type === "statement") return <Database className={c} />
  return <Package className={c} />
}

function Row({ n, self, onOpen }: { n: Node; self?: boolean; onOpen?: (n: Node) => void }) {
  return (
    <button type="button" disabled={self || !onOpen} onClick={() => onOpen?.(n)}
      className={cn("flex w-full items-center gap-2 rounded-md border px-2.5 py-1.5 text-left text-[12.5px]",
        self ? "border-foreground/30 bg-muted/60 font-medium" : "bg-card enabled:hover:bg-muted/50")}>
      <NodeIcon type={n.type} />
      <span className="min-w-0 flex-1 truncate font-mono">{n.label ?? n.id}</span>
      <FreshnessChip f={n.freshness ?? undefined} />
    </button>
  )
}

/** Inputs and documents above, this artifact in the middle, what was built from it below. */
export function LineageView({ refId, onOpen }: { refId: string; onOpen?: (node: { id: string; type: string }) => void }) {
  const up = useLineage(refId, "up")
  const down = useLineage(refId, "down")
  const others = (l?: Lineage) => (l?.nodes ?? []).filter((n) => n.id !== refId)
  const self: Node = up.data?.nodes.find((n) => n.id === refId) ?? down.data?.nodes.find((n) => n.id === refId) ?? { id: refId, type: "artifact" }
  const failed = up.error ?? down.error
  return (
    <div className="h-full overflow-auto p-4">
      {(up.isLoading || down.isLoading) && <div className="mb-3 flex items-center gap-2 text-[12.5px] text-muted-foreground"><Spinner className="size-3.5" />Loading lineage…</div>}
      {failed ? <p className="mb-3 text-[12.5px] text-status-bad">Lineage could not be loaded: {(failed as Error).message}</p> : null}
      <Section title="Built from" empty="No recorded inputs" nodes={others(up.data)} onOpen={onOpen} />
      <div className="my-2 flex justify-center text-muted-foreground"><ArrowDown className="size-3.5" /></div>
      <Row n={self} self />
      <div className="my-2 flex justify-center text-muted-foreground"><ArrowDown className="size-3.5" /></div>
      <Section title="Used by" empty="Nothing is built from this yet" nodes={others(down.data)} onOpen={onOpen} />
    </div>
  )
}

function Section({ title, empty, nodes, onOpen }: { title: string; empty: string; nodes: Node[]; onOpen?: (n: Node) => void }) {
  return (
    <section>
      <h3 className="mb-1.5 text-[11.5px] font-medium tracking-wide text-muted-foreground uppercase">{title} · {nodes.length}</h3>
      {nodes.length === 0 ? <p className="text-[12.5px] text-muted-foreground">{empty}</p>
        : <div className="space-y-1">{nodes.map((n) => <Row key={n.id} n={n} onOpen={onOpen} />)}</div>}
    </section>
  )
}
