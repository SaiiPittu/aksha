import { useEffect, useRef, useState } from "react"
import { ArrowUp, Bot, FolderUp, Lock, Paperclip, X } from "lucide-react"
import { isApiError } from "@/api"
import { cn } from "@/lib/utils"
import { Button } from "@/components/ui/button"
import { Kbd } from "@/components/ui/kbd"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Spinner } from "@/components/ui/spinner"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import type { Lane, SendInput, UploadedFile, UploadProgress, ViewRef } from "@/features/session/types"
import { uploadPath } from "@/features/session/commands"
import { Glyph } from "./bits"

const lanes: { key: Lane; label: string; hint: string }[] = [
  { key: "steer", label: "Steer this run", hint: "The agent reads it at its next model call and adjusts course." },
  { key: "follow_up", label: "Follow-up", hint: "Held until the run proposes to finish, then read." },
  { key: "amend", label: "Amend answer", hint: "Corrects your last answer to one of its questions." },
]

export type Upload = (files: File[], onProgress?: (p: UploadProgress[]) => void) => Promise<UploadedFile[]>

/** One attachment chip: uploading with progress, stored (solid), or refused with its code. */
interface Chip {
  key: string
  path: string
  state: "uploading" | "done" | "refused"
  loaded: number
  total: number
  file?: UploadedFile
  code?: string
}

const errorOf = (e: unknown) => (isApiError(e) ? { code: e.code, message: e.message } : { code: "error", message: String((e as Error)?.message ?? e) })

/**
 * M7 composer: Empty, Typing, Uploading, LaneChoice, Sending, and Disabled with its reason.
 * A refused send keeps the text and shows the server's code. A message refused with
 * `lane_required` (the run was open after all) asks for a lane and keeps the text.
 */
const AUTO = "auto"

/** An agent the composer can address: its name and purpose line (from the capability record). */
export interface AgentOption { name: string; purpose?: string }

export function Composer({ onSend, onUpload, runOpen, disabledReason, draft, dropTarget, agents, agent: fixedAgent }: {
  onSend: (input: SendInput) => Promise<void>
  onUpload?: Upload
  runOpen: boolean
  /** When set, the composer is locked and says why (for example: read only). */
  disabledReason?: string
  /** Text to seed the composer with, such as a quote from "Ask about this". */
  draft?: string
  /** The area where dropped files are attached (the whole chat column). */
  dropTarget?: React.RefObject<HTMLElement | null>
  /** Agents to pick from for a new run; none: the message is routed. */
  agents?: AgentOption[]
  /** The agent the session already works with, preselected. */
  agent?: string
}) {
  // The session's own agent is the default once the agent list has loaded; a pick overrides it.
  const [picked, setAgent] = useState<string | undefined>()
  const agent = picked ?? (fixedAgent && agents?.some((a) => a.name === fixedAgent) ? fixedAgent : AUTO)
  const folder = useRef<HTMLInputElement>(null)
  const [text, setText] = useState(draft ?? "")
  const [lane, setLane] = useState<Lane | undefined>()
  const [sending, setSending] = useState(false)
  const [refusal, setRefusal] = useState<{ code: string; message: string } | undefined>()
  const [laneRequired, setLaneRequired] = useState(false)
  const [chips, setChips] = useState<Chip[]>([])
  const [dragging, setDragging] = useState(false)
  const input = useRef<HTMLInputElement>(null)
  const askLane = runOpen || laneRequired
  const choosing = askLane && !lane && text.trim().length > 0
  const uploading = chips.some((c) => c.state === "uploading")
  const canSend = !disabledReason && text.trim().length > 0 && !sending && !choosing && !uploading

  const attach = (files: File[]) => {
    if (!onUpload || !files.length) return
    const batch = files.map((f, n) => ({ key: `${Date.now()}-${n}-${f.name}`, path: uploadPath(f), state: "uploading" as const, loaded: 0, total: f.size }))
    setChips((c) => [...c.filter((x) => !batch.some((b) => b.path === x.path)), ...batch])
    const keys = new Set(batch.map((b) => b.key))
    const mine = (c: Chip) => keys.has(c.key)
    onUpload(files, (progress) => setChips((cs) => cs.map((c) => {
      if (!mine(c)) return c
      const p = progress.find((x) => x.path === c.path)
      return p ? { ...c, loaded: p.loaded, total: p.total } : c
    }))).then((stored) => setChips((cs) => cs.map((c) => {
      if (!mine(c)) return c
      const f = stored.find((x) => x.path === c.path)
      return { ...c, state: "done", loaded: c.total, file: f }
    }))).catch((e) => {
      const { code } = errorOf(e)
      setChips((cs) => cs.map((c) => (mine(c) ? { ...c, state: "refused", code } : c)))
    })
  }

  // Drag and drop onto the chat column.
  const attachRef = useRef(attach)
  useEffect(() => { attachRef.current = attach })
  useEffect(() => {
    const el = dropTarget?.current
    if (!el || !onUpload || disabledReason) return
    const hasFiles = (e: DragEvent) => Array.from(e.dataTransfer?.types ?? []).includes("Files")
    const over = (e: DragEvent) => { if (hasFiles(e)) { e.preventDefault(); setDragging(true) } }
    const leave = (e: DragEvent) => { if (!el.contains(e.relatedTarget as Node | null)) setDragging(false) }
    const drop = (e: DragEvent) => {
      if (!hasFiles(e)) return
      e.preventDefault(); setDragging(false)
      attachRef.current(Array.from(e.dataTransfer?.files ?? []))
    }
    el.addEventListener("dragover", over); el.addEventListener("dragleave", leave); el.addEventListener("drop", drop)
    return () => { el.removeEventListener("dragover", over); el.removeEventListener("dragleave", leave); el.removeEventListener("drop", drop) }
  }, [dropTarget, onUpload, disabledReason])

  if (disabledReason) {
    return (
      <div className="flex min-h-[52px] items-center gap-3 rounded-xl border border-dashed bg-muted/40 px-4 py-3 text-[13px] text-muted-foreground">
        <Lock className="size-4 shrink-0" />
        <span className="flex-1">{disabledReason}</span>
      </div>
    )
  }

  const send = async () => {
    if (!canSend) return
    setSending(true); setRefusal(undefined)
    const files: ViewRef[] = chips.flatMap((c) => (c.state === "done" && c.file ? [c.file.view] : []))
    try {
      await onSend({ text: text.trim(), lane: askLane ? lane : undefined, files: files.length ? files : undefined,
        agent: !askLane && agent !== AUTO ? agent : undefined })
      setText(""); setLane(undefined); setLaneRequired(false); setChips([])
    } catch (e) {
      const err = errorOf(e)
      if (err.code === "lane_required") {
        // The run is open after all: keep the text and ask where it goes (M7 LaneChoice).
        setLaneRequired(true); setLane(undefined)
      } else setRefusal(err)
    } finally { setSending(false) }
  }

  return (
    <div className="space-y-2">
      {choosing && (
        <div className="flex items-center gap-2 px-1 text-[13px]" role="status">
          <span aria-hidden className="text-[10px] text-status-wait">◆</span>
          <span className="font-medium">Pick how this message applies</span>
          <span className="text-muted-foreground">A run is open, so Aksha needs to know where it goes.</span>
        </div>
      )}
      {refusal && (
        <div role="alert" className="flex items-start gap-2 rounded-md bg-status-bad-soft px-3 py-2 text-[13px] text-status-bad">
          <Glyph status="bad" className="mt-[5px]" />
          <span><span className="font-mono text-[12px] font-medium">{refusal.code}</span> · {refusal.message}</span>
        </div>
      )}
      <div className={cn("rounded-xl border bg-card shadow-xs transition-shadow focus-within:border-ring focus-within:ring-3 focus-within:ring-ring/30",
        choosing && "border-status-wait/50", dragging && "border-status-run border-dashed ring-3 ring-status-run/25")}>
        {dragging && <div className="px-3.5 pt-2.5 text-[12.5px] text-status-run">Drop to attach. A file with an existing name uploads as its next version.</div>}
        {chips.length > 0 && <Chips chips={chips} onRemove={(key) => setChips((cs) => cs.filter((x) => x.key !== key))} />}
        <textarea value={text} onChange={(e) => setText(e.target.value)} rows={2} aria-label="Message"
          onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); void send() } }}
          onPaste={(e) => { const fs = Array.from(e.clipboardData?.files ?? []); if (fs.length && onUpload) { e.preventDefault(); attach(fs) } }}
          placeholder={askLane ? "Message the agent while it works…" : "Ask anything."}
          className="block max-h-48 min-h-[56px] w-full resize-none bg-transparent px-3.5 pt-3 pb-1 text-[14px] leading-6 outline-none placeholder:text-muted-foreground" />
        <div className="flex items-center gap-1.5 px-2 pb-2">
          <input ref={input} type="file" multiple hidden aria-hidden tabIndex={-1} data-testid="attach-files-input"
            onChange={(e) => { attach(Array.from(e.target.files ?? [])); e.target.value = "" }} />
          <Tooltip>
            <TooltipTrigger asChild>
              <span><Button variant="ghost" size="icon-sm" aria-label="Attach files" className="text-muted-foreground" disabled={!onUpload}
                onClick={() => input.current?.click()}><Paperclip /></Button></span>
            </TooltipTrigger>
            <TooltipContent>{onUpload ? "Attach files, or drop them on the chat" : "Uploading is not available here"}</TooltipContent>
          </Tooltip>
          {/* A folder keeps each file's path inside it (records/a.csv), as `aksha run --input DIR` does. */}
          <input ref={folder} type="file" multiple hidden aria-hidden tabIndex={-1} data-testid="attach-folder-input"
            {...{ webkitdirectory: "", directory: "" }}
            onChange={(e) => { attach(Array.from(e.target.files ?? [])); e.target.value = "" }} />
          <Tooltip>
            <TooltipTrigger asChild>
              <span><Button variant="ghost" size="icon-sm" aria-label="Attach a folder" className="text-muted-foreground" disabled={!onUpload}
                onClick={() => folder.current?.click()}><FolderUp /></Button></span>
            </TooltipTrigger>
            <TooltipContent>Attach a folder: its files keep their paths inside it</TooltipContent>
          </Tooltip>
          {!askLane && agents && agents.length > 0 && (
            <Select value={agent} onValueChange={setAgent}>
              <SelectTrigger size="sm" aria-label="Agent" className="h-7 gap-1.5 border-none bg-transparent px-2 text-xs text-muted-foreground shadow-none hover:bg-accent hover:text-foreground">
                <Bot className="size-3.5" /><SelectValue>{agent === AUTO ? "Auto-route" : agent}</SelectValue>
              </SelectTrigger>
              <SelectContent align="start" className="max-w-[360px]">
                <SelectItem value={AUTO} className="text-[13px]">
                  <span className="flex flex-col"><span className="font-medium">Auto-route</span>
                    <span className="text-xs text-muted-foreground">Aksha picks the agent from your message</span></span>
                </SelectItem>
                {agents.map((a) => (
                  <SelectItem key={a.name} value={a.name} className="text-[13px]">
                    <span className="flex min-w-0 flex-col"><span className="font-medium">{a.name}</span>
                      {a.purpose && <span className="line-clamp-2 text-xs whitespace-normal text-muted-foreground">{a.purpose}</span>}</span>
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          )}
          {askLane && (
            <div role="radiogroup" aria-label="How this message applies"
              className={cn("flex items-center gap-0.5 rounded-lg border bg-muted/40 p-0.5", choosing && "border-status-wait/60 bg-status-wait-soft ring-2 ring-status-wait/25")}>
              {lanes.map((l) => (
                <Tooltip key={l.key}>
                  <TooltipTrigger asChild>
                    <button role="radio" aria-checked={lane === l.key} onClick={() => setLane(l.key)}
                      className={cn("h-6 rounded-md px-2 text-xs font-medium text-muted-foreground transition-colors hover:text-foreground",
                        lane === l.key && "bg-background text-foreground shadow-xs ring-1 ring-border")}>{l.label}</button>
                  </TooltipTrigger>
                  <TooltipContent>{l.hint}</TooltipContent>
                </Tooltip>
              ))}
            </div>
          )}
          <span className="ml-auto hidden items-center gap-1 text-xs text-muted-foreground md:inline-flex">
            {choosing ? "Pick a lane to send" : uploading ? "Uploading…" : <><Kbd>⇧⏎</Kbd> new line</>}
          </span>
          <Button size="sm" disabled={!canSend} className="ml-1 gap-1.5" onClick={() => void send()}>
            {sending ? <Spinner className="size-3.5" /> : <ArrowUp className="size-3.5" />}Send
            <Kbd className="bg-primary-foreground/15 text-primary-foreground">⏎</Kbd>
          </Button>
        </div>
      </div>
    </div>
  )
}

const FOLD_OVER = 8
const SHOW_FOLDED = 6

/** The attachment chips; a big batch (a folder) folds to a few, a summary and "show all". */
function Chips({ chips, onRemove }: { chips: Chip[]; onRemove: (key: string) => void }) {
  const [all, setAll] = useState(false)
  const fold = chips.length > FOLD_OVER && !all
  const done = chips.filter((c) => c.state === "done").length
  const changed = chips.filter((c) => c.file?.change === "changed").length
  const refused = chips.filter((c) => c.state === "refused").length
  return (
    <div className="px-3 pt-2.5">
      <ul aria-label="Attachments" className={cn("flex flex-wrap gap-1.5", all && "max-h-48 overflow-y-auto")}>
        {chips.map((c, n) => <AttachmentChip key={c.key} c={c} hidden={fold && n >= SHOW_FOLDED} onRemove={() => onRemove(c.key)} />)}
      </ul>
      {chips.length > FOLD_OVER && (
        <div className="mt-1.5 flex items-center gap-2 text-[12px] text-muted-foreground">
          <span>{chips.length} files · {done} stored{changed ? ` · ${changed} changed` : ""}{refused ? ` · ${refused} refused` : ""}</span>
          <button type="button" className="font-medium text-foreground underline-offset-2 hover:underline" onClick={() => setAll(!all)}>
            {fold ? `Show all ${chips.length}` : "Show fewer"}</button>
        </div>
      )}
    </div>
  )
}

function AttachmentChip({ c, onRemove, hidden }: { c: Chip; onRemove: () => void; hidden?: boolean }) {
  const pct = c.total ? Math.round((c.loaded / c.total) * 100) : 0
  const change = c.file?.change === "changed" ? `changed (v${c.file.from ?? c.file.to - 1} → v${c.file.to})`
    : c.file?.change === "unchanged" ? "unchanged, nothing to rebuild" : c.file ? `v${c.file.to}` : undefined
  return (
    <li hidden={hidden} className={cn("relative flex h-7 max-w-full items-center gap-1.5 overflow-hidden rounded-md border px-2 text-[12px]",
      c.state === "uploading" && "border-dashed text-muted-foreground", c.state === "done" && "bg-muted/50", c.state === "refused" && "border-status-bad/40 bg-status-bad-soft text-status-bad")}>
      {c.state === "uploading" && <span aria-hidden className="absolute inset-y-0 left-0 bg-status-run-soft" style={{ width: `${pct}%` }} />}
      <span className="relative flex min-w-0 items-center gap-1.5">
        {c.state === "uploading" ? <Spinner className="size-3" /> : <Glyph status={c.state === "done" ? (c.file?.change === "changed" ? "wait" : "ok") : "bad"} />}
        <span className="truncate font-mono">{c.path}</span>
        {c.state === "uploading" && <span className="tabular-nums">{pct}%</span>}
        {c.state === "done" && change && <span className={cn("shrink-0", c.file?.change === "changed" ? "text-status-wait" : "text-muted-foreground")}>{change}</span>}
        {c.state === "refused" && <span className="shrink-0 font-mono">{c.code}</span>}
      </span>
      {c.state !== "uploading" && (
        <button aria-label={`Remove ${c.path}`} onClick={onRemove} className="relative -mr-1 grid size-5 place-items-center rounded hover:bg-accent"><X className="size-3" /></button>
      )}
    </li>
  )
}
