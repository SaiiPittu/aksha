import { useEffect, useRef, useState } from "react"
import type { ReactNode } from "react"
import {
  Check, ChevronDown, Download, ExternalLink, GitCompare, Maximize2, MessageSquareQuote, Minimize2, Pin, PinOff, Radar, X,
} from "lucide-react"
import type { PanelMode, PanelState, ViewRef } from "@/features/session/types"
import { Button } from "@/components/ui/button"
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel, DropdownMenuRadioGroup, DropdownMenuRadioItem,
  DropdownMenuSeparator, DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { Kbd } from "@/components/ui/kbd"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { cn } from "@/lib/utils"
import { KindIcon, kindLabel, sourceMeta, viewToText } from "@/app/views/meta"
import { ViewRenderer } from "@/app/views/ViewRenderer"
import { DiffView } from "@/app/views/DiffView"
import { useCompare, useLoadedView } from "@/app/views/load"
import { FreshnessChip, LineageView } from "@/app/views/LineageView"
import { Spinner } from "@/components/ui/spinner"
import { artifactView } from "@/features/session/reducer"

/**
 * M8 View panel. Seeded from `panel`, then clickable on its own (modes, tabs, versions, compare).
 * Peek overlays the thread (no scrim), pinned is a flex child with a drag handle, fullscreen covers
 * the main area. Peek and fullscreen are absolutely positioned, so the parent must be `relative`.
 */
export function ViewPanel({ panel, onChange, onClose, onModeChange, onActivate, versionText, onAsk }: {
  panel: PanelState
  /** The text of another version of a view, for "Diff with…"; undefined while not loaded. */
  versionText?: (view: ViewRef, version: string) => string | undefined
  /** "Ask about this": quote the view (or its selection) in the composer. */
  onAsk?: (view: ViewRef) => void
  /** Reports every change (mode, tabs, active tab, follow). When set, the parent also owns Esc and click-outside. */
  onChange?: (p: PanelState) => void
  onClose?: () => void
  onModeChange?: (mode: PanelMode) => void
  onActivate?: (id: string) => void
}) {
  // Re-seed when the caller passes a different panel (derived-state pattern, no effect needed).
  const seedKey = `${panel.mode}|${panel.active}|${panel.tabs.map((t) => t.id).join(",")}`
  const [seed, setSeed] = useState(seedKey)
  const [mode, setModeRaw] = useState<PanelMode>(panel.mode)
  const [tabs, setTabs] = useState<ViewRef[]>(panel.tabs)
  const [active, setActiveRaw] = useState(panel.active ?? panel.tabs[0]?.id)
  const [follow, setFollow] = useState(!!panel.followAgent)
  const [width, setWidth] = useState<number | null>(null)
  const [versionOf, setVersionOf] = useState<Record<string, string>>({})
  const [compare, setCompare] = useState<Record<string, string | undefined>>({})
  const [prevMode, setPrevMode] = useState<PanelMode>(panel.mode === "fullscreen" ? "pinned" : panel.mode)
  if (seed !== seedKey) {
    setSeed(seedKey); setModeRaw(panel.mode); setTabs(panel.tabs); setActiveRaw(panel.active ?? panel.tabs[0]?.id); setCompare({})
  }
  const ref = useRef<HTMLElement>(null)

  const controlled = !!onChange
  const emit = (p: Partial<PanelState>) => {
    const next: PanelState = { mode, tabs, active, followAgent: follow, ...p }
    // Keep our seed in step so the echo from a controlling parent does not reset local state.
    setSeed(`${next.mode}|${next.active}|${next.tabs.map((t) => t.id).join(",")}`)
    onChange?.(next)
  }
  const setMode = (m: PanelMode, extra: Partial<PanelState> = {}) => {
    if (mode !== "fullscreen" && m === "fullscreen") setPrevMode(mode)
    setModeRaw(m)
    emit({ mode: m, ...extra })
    if (m === "closed") onClose?.()
    onModeChange?.(m)
  }
  const activate = (id: string) => { setActiveRaw(id); emit({ active: id }); onActivate?.(id) }
  const closeTab = (id: string) => {
    const i = tabs.findIndex((t) => t.id === id), next = tabs.filter((t) => t.id !== id)
    setTabs(next)
    if (!next.length) { setMode("closed", { tabs: next }); return }
    const nextActive = id === active ? next[Math.min(i, next.length - 1)].id : active
    setActiveRaw(nextActive); emit({ tabs: next, active: nextActive })
    if (id === active && nextActive) onActivate?.(nextActive)
  }

  const view = tabs.find((t) => t.id === active) ?? tabs[0]
  const comparing = view ? compare[view.id] : undefined
  const open = mode !== "closed" ? view : undefined
  const shownVersion = view ? versionOf[view.id] ?? view.version : undefined
  // The content, versions and freshness come from the reads API when the view opens.
  const loaded = useLoadedView(open, shownVersion)
  const cmp = useCompare(open, shownVersion, comparing)
  const [pane, setPane] = useState<Record<string, "content" | "lineage" | "citations">>({})

  // Keyboard (M8): Esc leaves compare first, then fullscreen goes to pinned, peek closes; pinned stays.
  // A controlling parent handles the mode part of Esc itself; we still own compare.
  useEffect(() => {
    if (mode === "closed") return
    const typing = (e: KeyboardEvent) => e.target instanceof HTMLElement && (e.target.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName))
    const on = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        if (document.querySelector("[data-slot=dropdown-menu-content]")) return
        if (view && compare[view.id]) { e.stopImmediatePropagation(); setCompare((c) => ({ ...c, [view.id]: undefined })) }
        else if (controlled) return
        else if (mode === "fullscreen") setMode("pinned")
        else if (mode === "peek") setMode("closed")
      } else if (!typing(e) && !e.metaKey && !e.ctrlKey && !e.altKey) {
        if (e.key === "f") setMode(mode === "fullscreen" ? prevMode : "fullscreen")
        if (e.key === "p" && mode !== "fullscreen") setMode(mode === "pinned" ? "peek" : "pinned")
      }
    }
    window.addEventListener("keydown", on, true)
    return () => window.removeEventListener("keydown", on, true)
  })

  // Peek: a click on the thread (inside our parent, outside the panel) closes it.
  useEffect(() => {
    if (mode !== "peek" || controlled) return
    const on = (e: MouseEvent) => {
      const el = ref.current, t = e.target as Node
      if (el && !el.contains(t) && el.parentElement?.contains(t)) setMode("closed")
    }
    document.addEventListener("mousedown", on)
    return () => document.removeEventListener("mousedown", on)
  })

  // Drag to resize; in peek, dragging wider than 40% of the area pins the panel (M8).
  const startDrag = (e: React.PointerEvent) => {
    const parent = ref.current?.parentElement
    if (!parent) return
    e.preventDefault()
    const box = parent.getBoundingClientRect()
    const move = (ev: PointerEvent) => {
      const w = Math.min(box.width - 360, Math.max(340, box.right - ev.clientX))
      setWidth(w)
      if (mode === "peek" && w > box.width * 0.4 + 40) setMode("pinned")
    }
    const up = () => { window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", up); document.body.style.cursor = "" }
    document.body.style.cursor = "col-resize"
    window.addEventListener("pointermove", move); window.addEventListener("pointerup", up)
  }

  const openTab = (v: ViewRef) => {
    const next = tabs.some((t) => t.id === v.id) ? tabs : [...tabs, v]
    setTabs(next); setActiveRaw(v.id); emit({ tabs: next, active: v.id }); onActivate?.(v.id)
  }

  if (mode === "closed" || !view) return null

  const full = loaded?.view ?? view
  const latest = full.versions?.at(-1) ?? full.version
  const shown = shownVersion
  const others = (full.versions ?? []).filter((v) => v !== shown)
  const earlier = comparing ? versionText?.(view, comparing) ?? cmp.before : undefined
  const later = comparing ? cmp.after ?? viewToText(full) ?? "" : ""
  const isArtifact = view.source === "artifact" && !!view.ref && view.kind !== "diff"
  const tab = isArtifact ? pane[view.id] ?? "content" : "content"
  const cites = full.citations ?? []

  return (
    <aside
      ref={ref}
      aria-label="View panel"
      data-mode={mode}
      style={mode === "fullscreen" ? undefined : { width: width ? `${width}px` : "var(--panel-width)" }}
      className={cn(
        "@container/panel flex min-h-0 min-w-0 flex-col bg-background text-[13px]",
        mode === "peek" && "absolute inset-y-0 right-0 z-30 border-l shadow-[-16px_0_40px_-16px_color-mix(in_oklab,var(--foreground)_22%,transparent)] animate-in duration-200 ease-out slide-in-from-right-12 fade-in-0",
        mode === "pinned" && "relative h-full shrink-0 border-l",
        mode === "fullscreen" && "absolute inset-0 z-40 animate-in duration-150 fade-in-0 zoom-in-[0.985]",
      )}
    >
      {mode !== "fullscreen" && (
        <div onPointerDown={startDrag} role="separator" aria-orientation="vertical" aria-label="Resize panel"
          className="group absolute inset-y-0 -left-1 z-20 flex w-2 cursor-col-resize items-center justify-center">
          <span className="h-full w-px bg-transparent transition-colors group-hover:bg-ring" />
          <span className="absolute top-1/2 h-8 w-1 -translate-y-1/2 rounded-full bg-border transition-colors group-hover:bg-ring" />
        </div>
      )}

      {/* Row 1: tabs and window controls */}
      <div className="flex h-9 shrink-0 items-stretch border-b bg-muted/40">
        <div role="tablist" className="flex min-w-0 flex-1 items-stretch overflow-x-auto [scrollbar-width:none] [mask-image:linear-gradient(to_right,black_calc(100%-28px),transparent)]">
          {tabs.map((t) => {
            const on = t.id === view.id
            return (
              <div key={t.id} role="tab" aria-selected={on} onClick={() => activate(t.id)}
                className={cn("group relative flex max-w-48 min-w-0 shrink-0 cursor-default items-center gap-1.5 border-r pr-1 pl-3 text-[12.5px]",
                  on ? "bg-background text-foreground" : "text-muted-foreground hover:bg-background/60 hover:text-foreground")}>
                {on && <span className="absolute inset-x-0 -bottom-px h-px bg-background" />}
                {on && <span className="absolute inset-x-0 top-0 h-0.5 bg-foreground/70" />}
                <KindIcon kind={t.kind} className="size-3.5 shrink-0 opacity-80" />
                <span className="truncate">{t.title}</span>
                <button aria-label={`Close ${t.title}`} onClick={(e) => { e.stopPropagation(); closeTab(t.id) }}
                  className={cn("ml-0.5 grid size-5 shrink-0 place-items-center rounded hover:bg-accent", on ? "opacity-70" : "opacity-0 group-hover:opacity-70")}>
                  <X className="size-3" />
                </button>
              </div>
            )
          })}
        </div>
        <div className="flex shrink-0 items-center gap-0.5 border-l px-1.5">
          <Tip label={follow ? "Following the agent: new views open here" : "Follow agent: open new views as they appear"}>
            <Button variant="ghost" size="icon-xs" aria-label="Follow agent" aria-pressed={follow} onClick={() => { setFollow(!follow); emit({ followAgent: !follow }) }}
              className={cn(follow ? "bg-status-run-soft text-status-run hover:bg-status-run-soft" : "text-muted-foreground")}><Radar /></Button>
          </Tip>
          {mode !== "fullscreen" && (
            <Tip label={mode === "pinned" ? "Unpin (back to peek)" : "Pin beside the chat"} kbd="P">
              <Button variant="ghost" size="icon-xs" className="text-muted-foreground" aria-label={mode === "pinned" ? "Unpin panel" : "Pin panel"} onClick={() => setMode(mode === "pinned" ? "peek" : "pinned")}>
                {mode === "pinned" ? <PinOff /> : <Pin />}
              </Button>
            </Tip>
          )}
          <Tip label={mode === "fullscreen" ? "Exit fullscreen" : "Fullscreen"} kbd={mode === "fullscreen" ? "Esc" : "F"}>
            <Button variant="ghost" size="icon-xs" className="text-muted-foreground" aria-label={mode === "fullscreen" ? "Exit fullscreen" : "Fullscreen"} onClick={() => setMode(mode === "fullscreen" ? prevMode : "fullscreen")}>
              {mode === "fullscreen" ? <Minimize2 /> : <Maximize2 />}
            </Button>
          </Tip>
          <Tip label="Close panel" kbd={mode === "peek" ? "Esc" : undefined}>
            <Button variant="ghost" size="icon-xs" className="text-muted-foreground" aria-label="Close panel" onClick={() => setMode("closed")}><X /></Button>
          </Tip>
        </div>
      </div>

      {/* Row 2: title, source, version, actions */}
      <div className="flex h-11 shrink-0 items-center gap-2 border-b px-3">
        <span className="grid size-6 shrink-0 place-items-center rounded-md border bg-muted/50 text-muted-foreground"><KindIcon kind={view.kind} /></span>
        <div className="flex min-w-0 items-center gap-2">
          <h2 className="truncate text-[13.5px] font-semibold">{view.title}</h2>
          <SourceBadge view={view} />
          <FreshnessChip f={full.freshness} />
          {full.versions && full.versions.length > 0 && (
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button variant="outline" size="xs" className="h-5! gap-0.5 px-1.5! font-mono text-[11px]!">{shown}<ChevronDown className="size-3" /></Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="start" className="w-44">
                <DropdownMenuLabel className="text-xs">Versions</DropdownMenuLabel>
                <DropdownMenuRadioGroup value={shown} onValueChange={(v) => setVersionOf((s) => ({ ...s, [view.id]: v }))}>
                  {[...full.versions].reverse().map((v) => (
                    <DropdownMenuRadioItem key={v} value={v} className="font-mono text-xs">
                      {v}{v === latest && <span className="ml-auto font-sans text-muted-foreground">latest</span>}
                    </DropdownMenuRadioItem>
                  ))}
                </DropdownMenuRadioGroup>
              </DropdownMenuContent>
            </DropdownMenu>
          )}
        </div>
        <div className="ml-auto flex shrink-0 items-center gap-0.5">
          <DropdownMenu>
            <Tip label={others.length ? "Compare with another version" : "Only one version so far"}>
              <span>
                <DropdownMenuTrigger asChild disabled={!others.length || view.kind === "diff"}>
                  <Button variant="ghost" size="xs" className="text-muted-foreground"><GitCompare /><span className="hidden @[760px]/panel:inline">Diff with…</span></Button>
                </DropdownMenuTrigger>
              </span>
            </Tip>
            <DropdownMenuContent align="end" className="w-48">
              <DropdownMenuLabel className="text-xs">Compare {shown} with</DropdownMenuLabel>
              {others.map((o) => (
                <DropdownMenuItem key={o} className="font-mono text-xs" onSelect={() => setCompare((c) => ({ ...c, [view.id]: o }))}>{o}</DropdownMenuItem>
              ))}
              <DropdownMenuSeparator />
              <DropdownMenuItem disabled className="text-xs">Another file…</DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
          <AskButton onAsk={onAsk ? () => onAsk(view) : undefined} />
          <Tip label={`Download ${view.title}`}>
            <Button variant="ghost" size="icon-xs" className="text-muted-foreground" aria-label="Download"><Download /></Button>
          </Tip>
          <Tip label="Open in new tab">
            <Button variant="ghost" size="icon-xs" className="text-muted-foreground" aria-label="Open in new tab"><ExternalLink /></Button>
          </Tip>
        </div>
      </div>

      {comparing && (
        <Bar tone="run">
          <GitCompare className="size-3.5" />Comparing <b className="font-mono font-medium">{comparing}</b> → <b className="font-mono font-medium">{shown}</b>
          <Button variant="ghost" size="xs" className="ml-auto h-6" onClick={() => setCompare((c) => ({ ...c, [view.id]: undefined }))}>Exit compare<Kbd>Esc</Kbd></Button>
        </Bar>
      )}
      {!comparing && shown !== latest && (
        <Bar tone="wait">
          <span aria-hidden className="text-[9px]">◆</span>Viewing <b className="font-mono font-medium">{shown}</b>, an older version · latest is <b className="font-mono font-medium">{latest}</b>
          <Button variant="ghost" size="xs" className="ml-auto h-6" onClick={() => setVersionOf((s) => ({ ...s, [view.id]: latest ?? "" }))}>Back to latest</Button>
        </Bar>
      )}

      {isArtifact && !comparing && (
        <div role="tablist" aria-label="Artifact" className="flex h-8 shrink-0 items-center gap-0.5 border-b px-2">
          {(["content", "lineage", "citations"] as const).map((k) => (
            <button key={k} role="tab" aria-selected={tab === k} onClick={() => setPane((p) => ({ ...p, [view.id]: k }))}
              className={cn("h-6 rounded-md px-2 text-[12px] font-medium text-muted-foreground hover:text-foreground", tab === k && "bg-muted text-foreground")}>
              {k === "content" ? "Content" : k === "lineage" ? "Lineage" : `Citations${cites.length ? ` ${cites.length}` : ""}`}
            </button>
          ))}
        </div>
      )}
      {loaded?.error && (
        <Bar tone="wait"><span aria-hidden className="text-[9px]">◆</span>Could not load this view: <span className="font-mono">{loaded.error.code}</span> · {loaded.error.message}</Bar>
      )}

      <div className="relative min-h-0 flex-1">
        {comparing
          ? earlier != null
            ? <DiffView before={earlier} after={later} label={view.title} fromLabel={comparing} toLabel={shown} tableable={full.kind === "table"} />
            : <Loading what="the other version" />
          : tab === "lineage" && view.ref
            ? <LineageView refId={`${view.ref.split("@")[0]}@${shown ?? "v1"}`} onOpen={(n) => { if (n.type === "artifact" && n.id.includes("@")) openTab(artifactView(n.id)) }} />
            : tab === "citations"
              ? <CitationList cites={cites} />
              : loaded?.loading ? <Loading what={view.title} />
                : <ViewRenderer key={`${view.id}|${shown}`} view={full} wide={mode === "fullscreen"} />}
      </div>
    </aside>
  )
}

function Loading({ what }: { what: string }) {
  return <div className="flex h-full items-center justify-center gap-2 text-[13px] text-muted-foreground"><Spinner className="size-3.5" />Loading {what}…</div>
}

function CitationList({ cites }: { cites: NonNullable<ViewRef["citations"]> }) {
  if (!cites.length) return <div className="grid h-full place-items-center text-[13px] text-muted-foreground">No citations are bound to this version.</div>
  return (
    <ol className="h-full space-y-2 overflow-auto p-4">
      {cites.map((c, i) => (
        <li key={i} className="rounded-lg border bg-card px-3 py-2 text-[12.5px]">
          <div className="flex items-center gap-2 font-mono text-[11.5px] text-muted-foreground">
            <span className="rounded bg-muted px-1">{i + 1}</span>{c.pointer}<span className="ml-auto">{c.target}{c.recordId && ` · ${c.recordId}`}</span>
          </div>
          {c.quote && <blockquote className="mt-1.5 border-l-2 border-status-wait pl-2.5 leading-5">{c.quote}</blockquote>}
        </li>
      ))}
    </ol>
  )
}

function SourceBadge({ view }: { view: ViewRef }) {
  const m = sourceMeta[view.source]
  const I = m.icon
  return (
    <Tip label={`${kindLabel(view.kind)} · ${view.id}`}>
      <span className="inline-flex h-5 shrink-0 items-center gap-1 rounded-full border px-1.5 text-[11px] text-muted-foreground">
        <I className="size-3" />{m.label}
      </span>
    </Tip>
  )
}

function AskButton({ onAsk }: { onAsk?: () => void }) {
  const [done, setDone] = useState(false)
  useEffect(() => { if (!done) return; const t = setTimeout(() => setDone(false), 1800); return () => clearTimeout(t) }, [done])
  return (
    <Tip label="Quote the selection (or this view) in the composer">
      <Button variant="ghost" size="xs" disabled={!onAsk} onClick={() => { onAsk?.(); setDone(true) }} className={cn(done ? "text-status-ok" : "text-muted-foreground")}>
        {done ? <Check /> : <MessageSquareQuote />}
        <span className="hidden @[520px]/panel:inline">{done ? "Quoted in composer" : "Ask about this"}</span>
      </Button>
    </Tip>
  )
}

function Bar({ tone, children }: { tone: "run" | "wait"; children: ReactNode }) {
  return (
    <div className={cn("flex h-9 shrink-0 items-center gap-1.5 border-b px-3 text-[12.5px]",
      tone === "run" ? "bg-status-run-soft text-status-run" : "bg-status-wait-soft text-status-wait")}>{children}</div>
  )
}

function Tip({ label, kbd, children }: { label: string; kbd?: string; children: ReactNode }) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>{children}</TooltipTrigger>
      <TooltipContent side="bottom">{label}{kbd && <Kbd>{kbd}</Kbd>}</TooltipContent>
    </Tooltip>
  )
}
