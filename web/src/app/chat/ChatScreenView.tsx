import { PanelRight, PanelRightClose, Square, WifiOff, MessageSquareText, Sheet, Globe, Bot } from "lucide-react"
import { useEffect, useRef, useState } from "react"
import { cn } from "@/lib/utils"
import { Button } from "@/components/ui/button"
import { Kbd } from "@/components/ui/kbd"
import { Spinner } from "@/components/ui/spinner"
import { SidebarTrigger } from "@/components/ui/sidebar"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { MessageScroller, MessageScrollerButton, MessageScrollerContent, MessageScrollerItem, MessageScrollerProvider, MessageScrollerViewport } from "@/components/ui/message-scroller"
import { Message, MessageContent, MessageHeader } from "@/components/ui/message"
import { StatusChip } from "@/app/shell/Status"
import { Banner, type BannerProps as ChatBanner } from "@/app/shell/Banner"
import { ViewPanel } from "@/app/panel/ViewPanel"
import type { Interrupt, Message as Msg, PanelState, Part, Run, SessionHandle, StreamState, ViewRef } from "@/features/session/types"
import { openRun } from "@/features/session/selectors"
import { AgentMark, type OpenView } from "./bits"
import { ArtifactChip, FileChips, Notice, StepGroup, TextPart, ToolRow, UiSpecPart } from "./Parts"
import { InterruptCard, type OnAct } from "./Interrupts"
import { Composer, type AgentOption } from "./Composer"


/** Shared width of the thread column and the composer: 720px, centred in whatever space is left of the panel. */
const column = "mx-auto w-full max-w-[720px] px-6"

/**
 * The chat screen. It renders a `SessionHandle` (state plus commands) and a controlled
 * panel; the route owns the panel (it mirrors it into `?view=`) and the session store owns
 * the state. `readOnly` locks every command and says why.
 */
export function ChatScreenView({ session, panel, onPanelChange, readOnly, banner, versionText, agents }: {
  session: SessionHandle
  panel: PanelState
  onPanelChange: (p: PanelState) => void
  readOnly?: string
  banner?: ChatBanner
  versionText?: (view: ViewRef, version: string) => string | undefined
  /** Agents the composer offers for a new run (from the capability record). */
  agents?: AgentOption[]
}) {
  const { state: s, commands } = session

  const openView: OpenView = (v: ViewRef) => {
    const p = panel
    const tabs = p.tabs.some((t) => t.id === v.id) ? p.tabs : [...p.tabs, v]
    onPanelChange({ ...p, mode: p.mode === "closed" ? "peek" : p.mode, tabs: p.mode === "closed" ? [v] : tabs, active: v.id })
  }
  const togglePanel = () => onPanelChange(panel.mode !== "closed" ? { ...panel, mode: "closed" }
    : panel.tabs.length ? { ...panel, mode: "pinned", active: panel.active ?? panel.tabs[0].id } : panel)

  useEffect(() => {
    const on = (e: KeyboardEvent) => {
      const typing = e.target instanceof HTMLElement && /^(INPUT|TEXTAREA)$/.test(e.target.tagName)
      if (e.key === "Escape") {
        if (panel.mode === "peek") onPanelChange({ ...panel, mode: "closed" })
        else if (panel.mode === "fullscreen") onPanelChange({ ...panel, mode: "pinned" })
      }
      if (e.key === "]" && !typing) togglePanel()
    }
    window.addEventListener("keydown", on)
    return () => window.removeEventListener("keydown", on)
  })

  // Peek closes on a click in the thread, unless the click opens another view.
  const onThreadDown = (e: React.MouseEvent) => {
    if (panel.mode === "peek" && !(e.target as HTMLElement).closest("[data-opens-view]")) onPanelChange({ ...panel, mode: "closed" })
  }

  const column$ = useRef<HTMLDivElement>(null)
  // M6: "Catching up" only when a replay lasts over 300 ms; the other states show at once.
  const replaying = s.stream === "replaying"
  const [slowReplay, setSlowReplay] = useState(false)
  useEffect(() => {
    if (!replaying) return
    const t = setTimeout(() => setSlowReplay(true), 300)
    return () => { clearTimeout(t); setSlowReplay(false) }
  }, [replaying])
  const showStream = s.stream === "reconnecting" || s.stream === "refetching" || (replaying && slowReplay)

  const run = openRun(s)
  const runOpen = !!run && (run.status === "run" || run.status === "wait")
  const empty = s.messages.length === 0
  const actFor = (i: Interrupt): OnAct | undefined => (readOnly ? undefined : (a) => commands.act(i.id, a))

  return (
    <div className="flex h-full min-h-0 flex-col bg-background">
      <ChatHeader title={s.title} agent={s.agent} run={run} panel={panel} hasTabs={panel.tabs.length > 0}
        onTogglePanel={togglePanel} onStop={readOnly || !run ? undefined : () => commands.stop(run.id)} />
      {banner && <Banner {...banner} />}
      {showStream && <StreamBanner state={s.stream} cursor={s.cursor} />}
      <div className="relative flex min-h-0 flex-1">
        <div ref={column$} className="flex min-w-0 flex-1 flex-col" onMouseDown={onThreadDown}>
          {empty ? <EmptyState loading={s.phase === "loading"} failed={s.phase === "not_found" || s.phase === "error" ? s.error : undefined} readOnly={!!readOnly} /> : (
            <Thread messages={s.messages} interrupts={s.interrupts} onOpenView={openView} actFor={actFor} dim={s.stream === "reconnecting"} />
          )}
          <div className={cn(column, "shrink-0 pt-2 pb-4")}>
            <WaitingStrip interrupts={s.interrupts} />
            <Composer onSend={commands.send} onUpload={commands.upload} runOpen={runOpen} disabledReason={readOnly} dropTarget={column$}
              agents={agents} agent={s.agent} />
            <p className="mt-2 text-center text-[11.5px] text-muted-foreground">
              Anything that leaves Aksha asks you first. Agents can be wrong; check the cited sources.
            </p>
          </div>
        </div>
        <ViewPanel panel={panel} onChange={onPanelChange} onClose={() => onPanelChange({ ...panel, mode: "closed" })} versionText={versionText} />
      </div>
    </div>
  )
}

// ---- what waits on the person ----------------------------------------------
const openStates = new Set<Interrupt["state"]>(["open", "submitting", "refused"])
const waitName: Record<Interrupt["kind"], string> = {
  input: "A question", choice: "A choice", route: "A routing question", budget: "A budget decision", review: "A review", approval: "An approval",
}

/**
 * A long run keeps streaming other Tasks' steps after it asks something, which pushes the
 * card out of sight: this line above the composer names what waits and jumps to its card.
 */
function WaitingStrip({ interrupts }: { interrupts: Interrupts }) {
  const open = Object.values(interrupts).filter((i) => openStates.has(i.state))
  if (!open.length) return null
  const jump = (id: string) => document.querySelector(`[data-interrupt-id="${CSS.escape(id)}"]`)?.scrollIntoView({ behavior: "smooth", block: "center" })
  return (
    <div role="status" aria-label="Waiting on you" className="mb-2 flex flex-wrap items-center gap-x-3 gap-y-1 rounded-lg border border-status-wait/40 bg-status-wait-soft px-3 py-1.5 text-[13px]">
      {open.map((i) => (
        <span key={i.id} className="flex min-w-0 items-center gap-2">
          <span aria-hidden className="text-[10px] text-status-wait">◆</span>
          <span className="truncate"><span className="font-medium">{waitName[i.kind]}</span> waits on you <span className="font-mono text-[11.5px] text-muted-foreground">{i.id}</span></span>
          <button className="shrink-0 text-xs font-medium underline-offset-2 hover:underline" onClick={() => jump(i.id)}>Jump to it</button>
        </span>
      ))}
    </div>
  )
}

// ---- header --------------------------------------------------------------
function ChatHeader({ title, agent, run: r, panel, hasTabs, onTogglePanel, onStop }: {
  title: string; agent?: string; run?: Run; panel: PanelState; hasTabs: boolean; onTogglePanel: () => void; onStop?: () => Promise<void>
}) {
  const stopping = r?.label === "Stop requested"
  return (
    <header className="flex h-12 shrink-0 items-center gap-2.5 border-b px-3">
      <SidebarTrigger className="text-muted-foreground" />
      <div className="h-4 w-px bg-border" />
      <h1 className="min-w-0 truncate text-[14px] font-medium">{title}</h1>
      <span className="inline-flex h-5 shrink-0 items-center gap-1 rounded-md border px-1.5 text-[11.5px] text-muted-foreground">
        <Bot className="size-3" />{r?.agent ?? agent ?? "Auto-route"}
      </span>
      {r && <span className="shrink-0 rounded-md bg-muted px-1.5 py-px font-mono text-[11.5px] text-muted-foreground">{r.id}</span>}
      {r && <StatusChip status={r.status}>{r.label}</StatusChip>}
      <div className="ml-auto flex shrink-0 items-center gap-2">
        {r && (
          <span className="hidden items-center gap-1.5 font-mono text-[11.5px] text-muted-foreground tabular-nums lg:inline-flex">
            <span>{r.turns} turns</span><span className="text-border">·</span><span>{r.tokens} tok</span><span className="text-border">·</span><span>{r.elapsed}</span>
          </span>
        )}
        {r?.status === "run" && !stopping && onStop && <Button size="sm" variant="outline" className="h-7" onClick={() => void onStop().catch(() => undefined)}><Square className="size-3 fill-current" />Stop run</Button>}
        {stopping && <Button size="sm" variant="outline" className="h-7" disabled><Spinner className="size-3" />Stopping…</Button>}
        <Tooltip>
          <TooltipTrigger asChild>
            <Button size="sm" variant={panel.mode === "closed" ? "ghost" : "secondary"} className="h-7 gap-1.5 text-muted-foreground" disabled={!hasTabs} onClick={onTogglePanel}>
              {panel.mode === "closed" ? <PanelRight className="size-3.5" /> : <PanelRightClose className="size-3.5" />}
              Panel
              {panel.tabs.length > 0 && <span className="rounded bg-muted px-1 font-mono text-[10.5px]">{panel.tabs.length}</span>}
              <Kbd>]</Kbd>
            </Button>
          </TooltipTrigger>
          <TooltipContent>{hasTabs ? "Show or hide the view panel" : "Nothing opened yet"}</TooltipContent>
        </Tooltip>
      </div>
    </header>
  )
}

// ---- stream banner -------------------------------------------------------
function StreamBanner({ state, cursor }: { state: StreamState; cursor: number | null }) {
  const text = state === "reconnecting"
    ? <><span className="font-medium">Reconnecting…</span> <span className="text-muted-foreground">Live updates paused. Nothing is lost: missed events replay{cursor != null ? ` from seq ${cursor + 1}` : ""} when the stream is back.</span></>
    : state === "replaying" ? <><span className="font-medium">Catching up</span> <span className="text-muted-foreground">Replaying events you missed…</span></>
      : <><span className="font-medium">Refreshing views</span> <span className="text-muted-foreground">Resynchronising with the server…</span></>
  return (
    <div role="status" className="flex min-h-9 shrink-0 items-center gap-2.5 border-b bg-muted/60 px-4 py-1.5 text-[13px]">
      {state === "reconnecting" ? <WifiOff className="size-3.5 shrink-0 text-muted-foreground" /> : <Spinner className="size-3.5 text-status-run" />}
      <span className="min-w-0 flex-1 truncate">{text}</span>
      {state === "reconnecting" && <Spinner className="size-3.5 text-muted-foreground" />}
    </div>
  )
}

// ---- thread --------------------------------------------------------------
type ActFor = (i: Interrupt) => OnAct | undefined
type Interrupts = Record<string, Interrupt>

function Thread({ messages, interrupts, onOpenView, actFor, dim }: { messages: Msg[]; interrupts: Interrupts; onOpenView: OpenView; actFor: ActFor; dim?: boolean }) {
  return (
    <MessageScrollerProvider defaultScrollPosition="end" autoScroll>
      <MessageScroller className="min-h-0 flex-1">
        <MessageScrollerViewport className="[scrollbar-gutter:stable_both-edges]">
          <MessageScrollerContent className={cn(column, "gap-7 pt-6 pb-4 transition-opacity", dim && "opacity-60")}>
            {messages.map((m) => (
              <MessageScrollerItem key={m.id} messageId={m.id}>
                <ThreadMessage m={m} ctx={{ interrupts, onOpenView, actFor }} />
              </MessageScrollerItem>
            ))}
          </MessageScrollerContent>
        </MessageScrollerViewport>
        <MessageScrollerButton />
      </MessageScroller>
    </MessageScrollerProvider>
  )
}

type Ctx = { interrupts: Interrupts; onOpenView: OpenView; actFor: ActFor }

const fmtAt = (iso: string) => {
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
}

function ThreadMessage({ m, ctx }: { m: Msg; ctx: Ctx }) {
  if (m.role === "user") {
    const texts = m.parts.filter((p) => p.type === "text")
    const rest = m.parts.filter((p) => p.type !== "text")
    return (
      <Message align="end" className={cn(m.pending && "opacity-70")}>
        <MessageContent className="items-end">
          <MessageHeader className="gap-1.5 px-0 font-normal">
            {m.lane && <span className="rounded-md border px-1.5 text-[11px] text-muted-foreground">{laneName[m.lane]}{m.laneState === "consumed" ? " · read by the run" : " · queued"}</span>}
            {m.author} · {m.pending ? "sending…" : fmtAt(m.at)}
          </MessageHeader>
          {texts.map((p, i) => (
            <div key={i} className="max-w-[85%] rounded-2xl rounded-tr-md bg-muted px-3.5 py-2.5 text-[14px] leading-6">{p.type === "text" && p.text}</div>
          ))}
          {rest.map((p, i) => <PartView key={i} p={p} ctx={ctx} align="end" />)}
        </MessageContent>
      </Message>
    )
  }
  if (m.role === "system") {
    return <div className="space-y-2 pl-9">{m.parts.map((p, i) => <PartView key={i} p={p} ctx={ctx} />)}</div>
  }
  const interruptOnly = m.parts.every((p) => p.type === "interrupt")
  return (
    <Message className="gap-3">
      <AgentMark name={m.author} className="mt-px" />
      <MessageContent className="gap-3">
        {!interruptOnly && <MessageHeader className="h-6 gap-1.5 px-0"><span className="text-foreground">{m.author}</span><span className="font-normal">· {fmtAt(m.at)}</span></MessageHeader>}
        {interruptOnly && <MessageHeader className="h-6 gap-1.5 px-0"><span className="text-foreground">{m.author}</span><span className="font-normal">· {fmtAt(m.at)} · asks</span></MessageHeader>}
        {m.parts.map((p, i) => <PartView key={i} p={p} ctx={ctx} />)}
      </MessageContent>
    </Message>
  )
}

function PartView({ p, ctx, align }: { p: Part; ctx: Ctx; align?: "end" }) {
  const { onOpenView } = ctx
  switch (p.type) {
    case "text": return <TextPart text={p.text} citations={p.citations} streaming={p.streaming} onOpenView={onOpenView} />
    case "tool": return <ToolRow t={p} onOpenView={onOpenView} />
    case "step-group": return <StepGroup label={p.label} steps={p.steps} saved={p.saved} onOpenView={onOpenView} />
    case "artifact": return <ArtifactChip view={p.view} note={p.note} onOpenView={onOpenView} />
    case "files": return <FileChips files={p.files} onOpenView={onOpenView} align={align} />
    case "ui-spec": return <UiSpecPart spec={p.spec} />
    case "notice": return <Notice tone={p.tone} text={p.text} />
    case "interrupt": {
      const i = ctx.interrupts[p.interruptId]
      return i ? <InterruptCard i={i} onOpenView={onOpenView} onAct={ctx.actFor(i)} /> : null
    }
  }
}

// ---- empty state ---------------------------------------------------------
const laneName = { steer: "Steer", follow_up: "Follow-up", amend: "Amend" } as const

function EmptyState({ loading, failed, readOnly }: { loading?: boolean; failed?: { code: string; message: string }; readOnly?: boolean }) {
  if (failed) return (
    <div className="grid min-h-0 flex-1 place-items-center px-6 text-center">
      <div><p className="text-[14px] font-medium">This session could not be opened</p>
        <p className="mt-1 text-[13px] text-muted-foreground"><span className="font-mono">{failed.code}</span> · {failed.message}</p></div>
    </div>
  )
  if (loading) return <div className="grid min-h-0 flex-1 place-items-center"><Spinner className="size-5 text-muted-foreground" /></div>
  if (readOnly) return (
    <div className="grid min-h-0 flex-1 place-items-center px-6 text-center">
      <div><p className="text-[14px] font-medium">Nothing open</p>
        <p className="mt-1 text-[13px] text-muted-foreground">Open a session from Sessions or Files to read it.</p></div>
    </div>
  )
  const starters = [
    { icon: MessageSquareText, title: "Summarise a document", hint: "Drop a PDF or Markdown file" },
    { icon: Sheet, title: "Clean up a spreadsheet", hint: "Attach a CSV; get a table back" },
    { icon: Globe, title: "Research on the web", hint: "With sources you can open" },
  ]
  return (
    <div className="flex min-h-0 flex-1 flex-col justify-end overflow-y-auto">
      <div className={cn(column, "pb-6")}>
        <div className="mb-1 text-[20px] font-semibold tracking-tight">What should we work on?</div>
        <p className="mb-5 text-[13.5px] text-muted-foreground">Ask anything. Tables, documents, diffs and pages the agent makes open in a panel on the right.</p>
        <div className="grid gap-2 sm:grid-cols-3">
          {starters.map((x) => (
            <button key={x.title} className="rounded-lg border bg-card px-3 py-2.5 text-left transition-colors hover:bg-muted/50">
              <x.icon className="mb-2 size-4 text-muted-foreground" />
              <div className="text-[13px] font-medium">{x.title}</div>
              <div className="text-xs text-muted-foreground">{x.hint}</div>
            </button>
          ))}
        </div>
      </div>
    </div>
  )
}
