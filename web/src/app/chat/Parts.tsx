import { Fragment, useState } from "react"
import { ChevronRight, ListTree, ArrowUpRight } from "lucide-react"
import { cn } from "@/lib/utils"
import { CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible"
import { HoverCardTrigger } from "@/components/ui/hover-card"
import { Spinner } from "@/components/ui/spinner"
import { Button } from "@/components/ui/button"
import { Attachment, AttachmentContent, AttachmentDescription, AttachmentGroup, AttachmentMedia, AttachmentTitle, AttachmentTrigger } from "@/components/ui/attachment"
import { Tool, ToolContent } from "@/components/ai-elements/tool"
import { Task, TaskTrigger } from "@/components/ai-elements/task"
import { InlineCitation, InlineCitationCard, InlineCitationCardBody } from "@/components/ai-elements/inline-citation"
import { UiSpec } from "@/app/views/UiSpec"
import type { Citation, Part, Status, ViewRef } from "@/features/session/types"
import { Glyph, ViewIcon, fmtMs, kindLabel, opensView, type OpenView } from "./bits"

type ToolPart = Extract<Part, { type: "tool" }>

// ---- text with [n] citation chips and a streaming caret ------------------
export function TextPart({ text, citations, streaming, onOpenView }: { text: string; citations?: Citation[]; streaming?: boolean; onOpenView: OpenView }) {
  const bits = text.split(/(\[\d+\])/g)
  return (
    <p className="text-[14px] leading-6 text-foreground">
      {bits.map((b, i) => {
        const m = b.match(/^\[(\d+)\]$/)
        const c = m && citations?.find((x) => x.n === Number(m[1]))
        if (!c) return <Fragment key={i}>{b}</Fragment>
        return <CitationChip key={i} c={c} onOpenView={onOpenView} />
      })}
      {streaming && <span aria-label="streaming" className="ml-0.5 inline-block h-[1.05em] w-[7px] translate-y-[3px] animate-pulse rounded-[1px] bg-status-run" />}
    </p>
  )
}

function CitationChip({ c, onOpenView }: { c: Citation; onOpenView: OpenView }) {
  return (
    <InlineCitation className="mx-px">
      <InlineCitationCard openDelay={150}>
        <HoverCardTrigger asChild>
          <button {...opensView} onClick={() => onOpenView(c.view)}
            className="relative -top-px inline-flex h-[18px] min-w-[18px] items-center justify-center rounded-[5px] border bg-muted px-1 align-middle font-mono text-[10.5px] font-medium text-muted-foreground transition-colors hover:border-foreground/30 hover:bg-accent hover:text-foreground">
            {c.n}
          </button>
        </HoverCardTrigger>
        <InlineCitationCardBody className="w-72 p-3">
          <div className="flex items-start gap-2.5">
            <span className="grid size-7 shrink-0 place-items-center rounded-md bg-muted text-muted-foreground"><ViewIcon kind={c.view.kind} className="size-3.5" /></span>
            <div className="min-w-0">
              <div className="truncate text-[13px] font-medium">{c.label}</div>
              <div className="text-xs text-muted-foreground">{kindLabel(c.view.kind)} · click to open in the panel</div>
            </div>
          </div>
          {c.view.highlight && <blockquote className="mt-2.5 line-clamp-3 border-l-2 border-status-wait pl-2.5 text-xs leading-5 text-muted-foreground">{c.view.highlight}</blockquote>}
        </InlineCitationCardBody>
      </InlineCitationCard>
    </InlineCitation>
  )
}

// ---- one tool call as a one-line, expandable row -------------------------
export function ToolRow({ t, onOpenView }: { t: ToolPart; onOpenView: OpenView }) {
  const bad = t.state === "refused" || t.state === "error"
  return (
    <Tool className="mb-0 rounded-md border-0">
      <div className="group/row flex h-7 items-center gap-1 rounded-md pr-1 hover:bg-muted/60">
        <CollapsibleTrigger className="flex h-full min-w-0 flex-1 items-center gap-2 pl-1.5 text-left text-[13px] outline-none">
          {t.state === "running" ? <Spinner className="size-3.5 shrink-0 text-status-run" /> : <Glyph status={bad ? "bad" : "ok"} />}
          <span className="shrink-0 font-mono text-[12.5px] text-foreground">{t.name}</span>
          <span className={cn("min-w-0 truncate text-muted-foreground", bad && "shrink-0 max-w-[40%]")}>{t.summary}</span>
          {t.state === "refused" && <span className="min-w-0 truncate text-status-bad"><span className="font-medium">Refused</span> · {t.refusal}</span>}
          {t.state === "error" && <span className="min-w-0 truncate text-status-bad"><span className="font-medium">Failed</span> · {t.refusal ?? "error"}</span>}
          <span className="ml-auto shrink-0 pl-2 font-mono text-[11.5px] text-muted-foreground tabular-nums">{t.state === "running" ? "running" : fmtMs(t.durationMs)}</span>
          <ChevronRight className="size-3.5 shrink-0 text-muted-foreground transition-transform group-data-[state=open]:rotate-90" />
        </CollapsibleTrigger>
        <span className="flex w-[54px] shrink-0 justify-end">
          {t.view && (
            <Button {...opensView} variant="ghost" size="xs" onClick={() => onOpenView(t.view!)}
              className="h-6 px-1.5 text-muted-foreground opacity-0 group-hover/row:opacity-100 focus-visible:opacity-100">
              Open<ArrowUpRight className="size-3" />
            </Button>
          )}
        </span>
      </div>
      <ToolContent className="space-y-2 px-0 pt-1 pb-2 pl-7">
        <dl className="grid grid-cols-[64px_1fr] gap-x-3 gap-y-1 text-xs">
          <dt className="text-muted-foreground">Input</dt>
          <dd className="font-mono text-foreground">{t.input ?? t.summary}</dd>
          {t.output && <><dt className="text-muted-foreground">Result</dt><dd className="font-mono whitespace-pre-wrap">{t.output}</dd></>}
          {t.refusal && <><dt className="text-muted-foreground">Refusal</dt><dd className="text-status-bad">{t.refusal} · nothing was sent</dd></>}
          {t.view && !t.output && <><dt className="text-muted-foreground">Result</dt>
            <dd><button {...opensView} onClick={() => onOpenView(t.view!)} className="inline-flex items-center gap-1 text-foreground underline-offset-2 hover:underline">
              <ViewIcon kind={t.view.kind} className="size-3.5" />{t.view.title}</button></dd></>}
        </dl>
      </ToolContent>
    </Tool>
  )
}

// ---- a collapsible group of steps ----------------------------------------
export function StepGroup({ label, steps, saved, onOpenView }: { label: string; steps: ToolPart[]; saved?: ViewRef[]; onOpenView: OpenView }) {
  const refused = steps.filter((s) => s.state === "refused" || s.state === "error").length
  const running = steps.some((s) => s.state === "running")
  // A finished Task folds to one line so a long run reads as a calm list; its saves stay
  // visible. It is open while it runs and folds when it finishes, unless the person toggled it.
  const [toggled, setToggled] = useState<boolean | undefined>()
  return (
    <div>
    <Task open={toggled ?? running} onOpenChange={setToggled}>
      <TaskTrigger title={label}>
        <button className="group flex w-full items-center gap-2 rounded-md py-0.5 text-left text-[13px] text-muted-foreground hover:text-foreground">
          <ChevronRight className="size-3.5 transition-transform group-data-[state=open]:rotate-90" />
          <ListTree className="size-3.5" />
          <span className="font-medium text-foreground">{label}</span>
          <span>· {steps.length} {steps.length === 1 ? "step" : "steps"}</span>
          {running ? <span className="text-status-run">· running</span>
            : refused > 0 ? <span className="text-status-bad">· {refused} refused</span> : <span className="text-status-ok">· done</span>}
        </button>
      </TaskTrigger>
      <CollapsibleContent>
        <div className="mt-1 ml-[6px] space-y-px border-l pl-2.5">
          {steps.map((s, i) => <ToolRow key={i} t={s} onOpenView={onOpenView} />)}
        </div>
      </CollapsibleContent>
    </Task>
    {saved && saved.length > 0 && (
      <div className="mt-1 ml-[22px] flex flex-wrap gap-1.5">
        {saved.map((v) => (
          <button key={v.id} {...opensView} onClick={() => onOpenView(v)} aria-label={`Open ${v.title}`}
            className="inline-flex h-6 max-w-full items-center gap-1.5 rounded-md border bg-card px-2 text-[12px] transition-colors hover:border-foreground/20 hover:bg-muted/50">
            <ViewIcon kind={v.kind} className="size-3.5 text-muted-foreground" />
            <span className="truncate font-mono">{v.title}</span>
            {v.version && <span className="font-mono text-muted-foreground">@{v.version}</span>}
            {v.citations?.length ? <span className="text-muted-foreground">· {v.citations.length} cited</span> : null}
          </button>
        ))}
      </div>
    )}
    </div>
  )
}

// ---- an artifact the agent saved -----------------------------------------
export function ArtifactChip({ view, note, onOpenView }: { view: ViewRef; note: string; onOpenView: OpenView }) {
  return (
    <button {...opensView} onClick={() => onOpenView(view)}
      className="group flex w-full items-center gap-3 rounded-lg border bg-card px-3 py-2 text-left transition-colors hover:border-foreground/20 hover:bg-muted/40">
      <span className="grid size-8 shrink-0 place-items-center rounded-md bg-muted text-muted-foreground"><ViewIcon kind={view.kind} /></span>
      <span className="min-w-0 flex-1">
        <span className="flex items-baseline gap-1.5">
          <span className="truncate font-mono text-[13px] font-medium">{view.title}</span>
          {view.version && <span className="font-mono text-xs text-muted-foreground">@{view.version}</span>}
          <span className="text-[11px] text-muted-foreground">· {kindLabel(view.kind)}</span>
        </span>
        <span className="block truncate text-xs text-muted-foreground">{note}</span>
      </span>
      <span className="inline-flex h-7 shrink-0 items-center gap-1 rounded-md border bg-background px-2 text-xs font-medium group-hover:bg-muted">
        Open<ArrowUpRight className="size-3" />
      </span>
    </button>
  )
}

// ---- input files attached to a message -----------------------------------
/** "v2", or "v1 → v2" for a file that has an earlier version. */
const changeOf = (f: ViewRef) => {
  const n = f.versions?.length ?? 0
  return n > 1 && f.version === f.versions![n - 1] ? `${f.versions![n - 2]} → ${f.version}` : f.version
}

export function FileChips({ files, onOpenView, align }: { files: ViewRef[]; onOpenView: OpenView; align?: "end" }) {
  return (
    <AttachmentGroup className={cn("flex-wrap gap-2 overflow-visible py-0", align === "end" && "justify-end")}>
      {files.map((f) => (
        <Attachment key={f.id} size="sm" className="min-w-0 rounded-lg">
          <AttachmentMedia className="size-7 text-muted-foreground"><ViewIcon kind={f.kind} className="size-3.5" /></AttachmentMedia>
          <AttachmentContent className="pr-1">
            <AttachmentTitle className="font-mono text-[12.5px]">{f.title}</AttachmentTitle>
            <AttachmentDescription>{kindLabel(f.kind)}{f.version && ` · ${changeOf(f)}`}</AttachmentDescription>
          </AttachmentContent>
          <AttachmentTrigger {...opensView} aria-label={`Open ${f.title}`} onClick={() => onOpenView(f)} />
        </Attachment>
      ))}
    </AttachmentGroup>
  )
}

// ---- a system line -------------------------------------------------------
export function Notice({ tone, text }: { tone: Status; text: string }) {
  return (
    <div className={cn("flex items-start gap-2 rounded-md border px-3 py-2 text-[13px]",
      tone === "wait" ? "border-status-wait/30 bg-status-wait-soft/60" : tone === "bad" ? "border-status-bad/30 bg-status-bad-soft/60"
        : tone === "ok" ? "border-status-ok/25 bg-status-ok-soft/50" : "bg-muted/40")}>
      <Glyph status={tone} className="mt-[5px]" />
      <span className="text-foreground">{text}</span>
    </div>
  )
}

// ---- generative UI, rendered with the app's own components ---------------
export function UiSpecPart({ spec }: { spec: Parameters<typeof UiSpec>[0]["spec"] }) {
  return <div className="min-w-0"><UiSpec spec={spec} /></div>
}
