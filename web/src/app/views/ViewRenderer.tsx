import { useState } from "react"
import type { BundledLanguage } from "shiki"
import { ImageIcon, Minus, Plus, Puzzle } from "lucide-react"
import type { ViewRef } from "@/features/session/types"
import { CodeBlock, CodeBlockCopyButton, CodeBlockFilename, CodeBlockHeader, CodeBlockTitle } from "@/components/ai-elements/code-block"
import { Button } from "@/components/ui/button"
import { Empty, EmptyContent, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty"
import { DiffView } from "./DiffView"
import { EmbedView, HtmlView, PdfFileView, WebView } from "./FrameViews"
import { MarkdownView } from "./MarkdownView"
import { PdfView } from "./PdfView"
import { TableView } from "./TableView"
import { TraceView } from "./TraceView"
import { UiSpec } from "./UiSpec"
import { JsonView } from "./JsonView"

// Picks the renderer by view.kind. Renderers ship in the shell; no agent code is needed.
// `wide` is true in fullscreen, where some renderers add side furniture (an outline).
export function ViewRenderer({ view, wide }: { view: ViewRef; wide?: boolean }) {
  return <div className="@container h-full min-h-0">{render(view, wide)}</div>
}

function render(v: ViewRef, wide?: boolean) {
  switch (v.kind) {
    case "markdown": return <MarkdownView markdown={v.markdown ?? ""} outline={wide} />
    case "pdf": return v.url && !v.pages
      ? <PdfFileView url={v.url} title={v.title} />
      : <PdfView pages={v.pages ?? []} highlight={v.highlight} title={v.title} />
    case "text": return <TextView view={v} />
    case "json": return v.json !== undefined ? <JsonView value={v.json} /> : <Missing what="content yet" />
    case "table": return v.table ? <TableView {...v.table} /> : <Missing what="rows" />
    case "diff": return v.diff
      ? <DiffView before={v.diff.before} after={v.diff.after} label={v.diff.label} fromLabel={v.versions?.[0]} toLabel={v.version} tableable={v.diff.label.endsWith(".csv") || v.diff.mode === "table"} />
      : <Missing what="versions" />
    case "image": return <ImageView view={v} />
    case "web": return <WebView url={v.url} html={v.html} />
    case "embed": return <EmbedView provider={v.embedProvider} url={v.url} title={v.title} />
    case "html": return <HtmlView html={v.html} />
    case "ui-spec": return v.spec ? <div className="h-full overflow-auto p-5"><UiSpec spec={v.spec} /></div> : <Missing what="spec" />
    case "trace": return <TraceView spans={v.spans ?? []} />
    default: return <PluginMissing kind={v.kind} />
  }
}

function TextView({ view }: { view: ViewRef }) {
  const t = view.text ?? { language: "text", body: "" }
  return (
    <div className="h-full overflow-auto p-4">
      <CodeBlock code={t.body} language={t.language as BundledLanguage} showLineNumbers className="text-[12.5px]">
        <CodeBlockHeader>
          <CodeBlockTitle><CodeBlockFilename>{view.title}</CodeBlockFilename><span className="text-muted-foreground/70">{t.language}</span></CodeBlockTitle>
          <CodeBlockCopyButton />
        </CodeBlockHeader>
      </CodeBlock>
    </div>
  )
}

function ImageView({ view }: { view: ViewRef }) {
  const [z, setZ] = useState(100)
  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex h-10 shrink-0 items-center gap-1 border-b px-3 text-[12.5px]">
        <Button variant="ghost" size="icon-xs" aria-label="Zoom out" onClick={() => setZ((x) => Math.max(25, x - 25))}><Minus /></Button>
        <span className="w-10 text-center tabular-nums">{z}%</span>
        <Button variant="ghost" size="icon-xs" aria-label="Zoom in" onClick={() => setZ((x) => Math.min(400, x + 25))}><Plus /></Button>
        <span className="ml-2 text-muted-foreground">Drag to pan</span>
      </div>
      <div className="grid min-h-0 flex-1 cursor-grab place-items-center overflow-auto bg-muted/60 bg-[conic-gradient(var(--muted)_25%,transparent_0_50%,var(--muted)_0_75%,transparent_0)] [background-size:16px_16px]">
        {view.url
          ? <img src={view.url} alt={view.title} style={{ width: `${z}%` }} className="max-w-none shadow-md" />
          : <div className="grid aspect-video w-2/3 place-items-center rounded-md border bg-card text-muted-foreground shadow-sm" style={{ scale: z / 100 }}><ImageIcon className="size-8" /></div>}
      </div>
    </div>
  )
}

function PluginMissing({ kind }: { kind: string }) {
  return (
    <Empty className="h-full">
      <EmptyHeader>
        <EmptyMedia variant="icon"><Puzzle /></EmptyMedia>
        <EmptyTitle>Plugin view not installed</EmptyTitle>
        <EmptyDescription>
          This item asks for the <code className="rounded bg-muted px-1 font-mono text-[12px]">{kind}</code> view, which no installed extension provides. You can still open the raw data.
        </EmptyDescription>
      </EmptyHeader>
      <EmptyContent className="flex-row justify-center gap-2">
        <Button size="sm" variant="outline">Open as table</Button>
        <Button size="sm" variant="ghost">Download</Button>
      </EmptyContent>
    </Empty>
  )
}

function Missing({ what }: { what: string }) {
  return <div className="grid h-full place-items-center text-[13px] text-muted-foreground">Nothing to show: this view has no {what}.</div>
}
