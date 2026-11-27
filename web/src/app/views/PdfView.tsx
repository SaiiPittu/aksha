import { useState } from "react"
import { ChevronDown, ChevronUp, Minus, Plus, Quote, ScanLine } from "lucide-react"
import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"

// A mock paged PDF viewer: page cards, a page-number gutter, zoom, and the cited span highlighted.

function Highlighted({ text, span }: { text: string; span?: string }) {
  if (!span || !text.includes(span)) return <>{text}</>
  const [a, b] = text.split(span)
  return (
    <>
      {a}
      <mark className="rounded-[2px] bg-status-wait-soft px-0.5 text-foreground underline decoration-status-wait/60 decoration-1 underline-offset-[3px] [box-decoration-break:clone]">{span}</mark>
      <span className="relative -top-[0.5em] ml-0.5 rounded-sm bg-status-wait px-1 py-px font-sans text-[9.5px] font-semibold text-background">1</span>
      {b}
    </>
  )
}

export function PdfView({ pages, highlight, title }: { pages: string[][]; highlight?: string; title: string }) {
  const [zoom, setZoom] = useState(100)
  const hitPage = pages.findIndex((p) => p.some((t) => highlight && t.includes(highlight)))
  const [page, setPage] = useState(Math.max(0, hitPage))
  const total = Math.max(pages.length, 14) // the mock shows the first pages of a longer document
  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex h-10 shrink-0 items-center gap-1 border-b px-3 text-[12.5px]">
        <Button variant="ghost" size="icon-xs" aria-label="Previous page" onClick={() => setPage((p) => Math.max(0, p - 1))}><ChevronUp /></Button>
        <Button variant="ghost" size="icon-xs" aria-label="Next page" onClick={() => setPage((p) => Math.min(pages.length - 1, p + 1))}><ChevronDown /></Button>
        <span className="ml-1 tabular-nums">
          <span className="inline-block min-w-6 rounded border bg-background px-1 text-center">{page + 1}</span>
          <span className="text-muted-foreground"> / {total}</span>
        </span>
        <span className="mx-2 h-4 w-px bg-border" />
        <Button variant="ghost" size="icon-xs" aria-label="Zoom out" onClick={() => setZoom((z) => Math.max(60, z - 10))}><Minus /></Button>
        <span className="w-10 text-center tabular-nums">{zoom}%</span>
        <Button variant="ghost" size="icon-xs" aria-label="Zoom in" onClick={() => setZoom((z) => Math.min(160, z + 10))}><Plus /></Button>
        <Button variant="ghost" size="xs" className="text-muted-foreground" onClick={() => setZoom(100)}><ScanLine />Fit width</Button>
        {highlight && (
          <span className="ml-auto inline-flex items-center gap-1.5 rounded-full bg-status-wait-soft px-2 py-0.5 text-[11.5px] text-status-wait">
            <Quote className="size-3" />Cited span · p.{hitPage + 1}
          </span>
        )}
      </div>
      <div className="min-h-0 flex-1 overflow-auto bg-muted/60">
        <div className="mx-auto flex flex-col gap-5 py-6 pr-8 pl-3" style={{ maxWidth: `${(zoom / 100) * 640}px` }}>
          {pages.map((paras, i) => (
            <div key={i} className="flex gap-3" onClick={() => setPage(i)}>
              <div className={cn("w-6 shrink-0 pt-1 text-right font-mono text-[11px] tabular-nums", i === page ? "text-foreground" : "text-muted-foreground/60")}>{i + 1}</div>
              <div
                className={cn("min-h-[420px] flex-1 rounded-sm border bg-card px-[9%] py-[7%] font-serif text-card-foreground shadow-sm",
                  i === page && "ring-1 ring-ring/40")}
                style={{ fontSize: `${(zoom / 100) * 12.5}px`, lineHeight: 1.6 }}
              >
                <div className="mb-[1.4em] flex justify-between font-sans text-[0.78em] text-muted-foreground"><span>{title}</span><span>Page {i + 1}</span></div>
                {paras.map((t, j) => (
                  <p key={j} className={cn("mb-[0.9em]", j === 0 && i === 0 && "text-center font-sans text-[1.05em] font-semibold tracking-wide", j === 0 && i > 0 && "font-sans font-semibold")}>
                    <Highlighted text={t} span={highlight} />
                  </p>
                ))}
                <div className="mt-[2em] space-y-[0.7em]" aria-hidden>
                  {[92, 86, 95, 70].map((w, k) => <div key={k} className="h-[0.55em] rounded-full bg-muted" style={{ width: `${w}%` }} />)}
                </div>
              </div>
            </div>
          ))}
          <div className="pl-9 text-center text-[11.5px] text-muted-foreground">Showing {pages.length} of {total} pages · scroll to load more</div>
        </div>
      </div>
    </div>
  )
}
