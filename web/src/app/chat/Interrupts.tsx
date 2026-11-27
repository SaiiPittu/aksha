import { useState } from "react"
import { ArrowUpRight, FileSignature, GitCompareArrows, Pencil, RotateCcw, Square, Flag, Plus } from "lucide-react"
import { cn } from "@/lib/utils"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Kbd } from "@/components/ui/kbd"
import { Spinner } from "@/components/ui/spinner"
import { StatusChip } from "@/app/shell/Status"
import type { Interrupt, InterruptAction, Status } from "@/features/session/types"
import { isApiError } from "@/api"
import { AgentMark, Glyph, LeavesBadge, opensView, type OpenView } from "./bits"

/** How a card reports a click: resolves on an accepted receipt, rejects with the refusal. */
export type OnAct = (action: InterruptAction) => Promise<void>

/** Local M4/M5 state for one card: submitting while the command is in flight, the refusal after. */
function useAct(i: Interrupt, onAct?: OnAct) {
  const [pending, setPending] = useState(false)
  const [refusal, setRefusal] = useState<{ code: string; message: string } | undefined>()
  const run = async (a: InterruptAction) => {
    if (!onAct || pending) return
    setPending(true); setRefusal(undefined)
    try { await onAct(a) } catch (e) {
      setRefusal(isApiError(e) ? { code: e.code, message: e.message } : { code: "error", message: String((e as Error)?.message ?? e) })
    } finally { setPending(false) }
  }
  return { submitting: pending || i.state === "submitting", refusal: refusal ?? i.refusal, run, disabled: !onAct }
}

const kindName: Record<Interrupt["kind"], string> = {
  input: "Question", choice: "Choice", route: "Route", budget: "Budget", review: "Review", approval: "Approval",
}

/** Card shell shared by every interrupt kind. */
function Shell({ tone = "idle", head, children, footer, className }: {
  tone?: Status; head: React.ReactNode; children?: React.ReactNode; footer?: React.ReactNode; className?: string
}) {
  return (
    <div className={cn("overflow-hidden rounded-xl border bg-card shadow-xs",
      tone === "wait" && "border-status-wait/45", tone === "ok" && "border-status-ok/40", tone === "bad" && "border-status-bad/40", className)}>
      <div className={cn("flex min-h-10 items-center gap-2 border-b px-4 py-2",
        tone === "wait" ? "border-status-wait/25 bg-status-wait-soft" : tone === "ok" ? "border-status-ok/20 bg-status-ok-soft"
          : tone === "bad" ? "border-status-bad/20 bg-status-bad-soft" : "bg-muted/50")}>{head}</div>
      {children && <div className="space-y-3 px-4 py-3.5">{children}</div>}
      {footer && <div className="flex flex-wrap items-center gap-2 border-t px-4 py-2.5">{footer}</div>}
    </div>
  )
}

const Id = ({ id, extra }: { id: string; extra?: string }) => (
  <span className="ml-auto shrink-0 font-mono text-[11.5px] text-muted-foreground">{extra && <>{extra} · </>}{id}</span>
)

/** A closed interrupt collapses to one line. */
function ClosedLine({ status, children, id }: { status: Status; children: React.ReactNode; id: string }) {
  return (
    <div className="flex items-center gap-2 rounded-lg border bg-muted/30 px-3 py-2 text-[13px]">
      <Glyph status={status} />
      <span className="min-w-0 flex-1 truncate">{children}</span>
      <span className="shrink-0 font-mono text-[11.5px] text-muted-foreground">{id}</span>
    </div>
  )
}

function Refusal({ code, message }: { code: string; message: string }) {
  return (
    <div className="flex items-start gap-2 rounded-md bg-status-bad-soft px-3 py-2 text-[13px] text-status-bad">
      <Glyph status="bad" className="mt-[5px]" />
      <span><span className="font-mono text-[12px] font-medium">{code}</span> · {message}</span>
    </div>
  )
}

function closedStates(i: Interrupt) {
  switch (i.state) {
    case "resolved": return <ClosedLine status="ok" id={i.id}><span className="text-muted-foreground">{i.title}</span>
      <span className="mx-1.5 text-muted-foreground">→</span><span className="font-medium">{i.answer}</span>
      {i.decidedBy && <span className="text-muted-foreground"> · answered by {i.decidedBy}</span>}</ClosedLine>
    case "resolved-elsewhere": return <ClosedLine status="ok" id={i.id}><span className="text-muted-foreground">{i.title}</span>
      <span className="mx-1.5 text-muted-foreground">·</span>Answered on another device{i.decidedBy && ` by ${i.decidedBy}`}{i.answer && <>: <span className="font-medium">{i.answer}</span></>}</ClosedLine>
    case "voided": return <ClosedLine status="idle" id={i.id}><span className="text-muted-foreground line-through">{i.title}</span>
      <span className="text-muted-foreground"> · voided{i.detail ? `: ${i.detail}` : ", the run ended"}</span></ClosedLine>
    case "expired": return <ClosedLine status="idle" id={i.id}><span className="text-muted-foreground">{i.title} · expired without an answer</span></ClosedLine>
    default: return null
  }
}

// ---- input, choice, route ------------------------------------------------
function QuestionCard({ i, onAct }: { i: Interrupt; onAct?: OnAct }) {
  const q = i.questions?.[0]
  const many = (i.questions?.length ?? 0) > 1 && !q?.options?.length
  const [sel, setSel] = useState<string | undefined>(q?.selected)
  const [free, setFree] = useState("")
  const [each, setEach] = useState<Record<string, string>>({})
  const act = useAct(i, onAct)
  const submitting = act.submitting
  const route = i.kind === "route"
  const picked = q?.options?.find((o) => o.key === sel)
  const ready = many ? (i.questions ?? []).every((x) => (each[x.id] ?? "").trim()) : !!sel || !!free.trim()
  const submit = () => {
    if (many) return act.run({ kind: "answer", answers: Object.fromEntries((i.questions ?? []).map((x) => [x.id, (each[x.id] ?? "").trim()])) })
    if (free.trim()) return act.run({ kind: "answer", text: free.trim() })
    if (!sel) return
    if (route) return act.run({ kind: "answer", agent: sel })
    return act.run({ kind: "answer", answers: q ? { [q.id]: sel } : undefined, text: q ? undefined : sel })
  }
  const onEnter = (e: React.KeyboardEvent) => { if (e.key === "Enter" && !e.nativeEvent.isComposing && ready && !submitting) { e.preventDefault(); void submit() } }
  return (
    <Shell tone="wait" head={<>
      <StatusChip status="wait">Waiting on you</StatusChip>
      <span className="text-[13px] font-medium">{kindName[i.kind]}</span>
      <Id id={i.id} />
    </>} footer={<>
      <span className="text-xs text-muted-foreground">{route ? "The chosen agent starts the run." : "Answering resumes the run."}</span>
      <Button size="sm" className="ml-auto" disabled={!ready || submitting || act.disabled} onClick={submit}>
        {submitting && <Spinner className="size-3.5" />}
        {route ? (picked && picked.key !== "other" ? `Route to ${picked.label}` : "Route") : "Submit"}
        <Kbd className="bg-primary-foreground/15 text-primary-foreground">⏎</Kbd>
      </Button>
    </>}>
      <div>
        <div className="text-[14px] font-medium">{i.title}</div>
        {i.detail && <p className="mt-0.5 text-[13px] leading-5 text-muted-foreground">{i.detail}</p>}
      </div>
      {act.refusal && <Refusal {...act.refusal} />}
      {many && (
        <div className="space-y-2.5">
          {i.questions!.map((x) => (
            <label key={x.id} className="block space-y-1">
              <span className="block text-[13px]">{x.text}</span>
              <Input value={each[x.id] ?? ""} onChange={(e) => setEach((m) => ({ ...m, [x.id]: e.target.value }))} onKeyDown={onEnter}
                disabled={act.disabled} className="h-8 text-[13px]" aria-label={x.text} />
            </label>
          ))}
        </div>
      )}
      {q && !many && (
        <div className="space-y-2">
          {!route && <div className="text-[13px]">{q.text}</div>}
          {route && <div className="text-[13px] text-muted-foreground">{q.text}</div>}
          <div role="radiogroup" className="grid gap-1.5">
            {q.options?.map((o, n) => {
              const on = sel === o.key
              return (
                <button key={o.key} role="radio" aria-checked={on} onClick={() => setSel(o.key)}
                  className={cn("flex items-center gap-3 rounded-lg border px-3 py-2 text-left text-[13px] transition-colors hover:bg-muted/50",
                    on && "border-foreground/40 bg-muted/60")}>
                  <Kbd>{n + 1}</Kbd>
                  {route && o.key !== "other" && <AgentMark name={o.label} className="size-5 text-[10px]" />}
                  <span className="font-medium">{o.label}</span>
                  {o.hint && <span className="min-w-0 truncate text-muted-foreground">{o.hint}</span>}
                  <span className={cn("ml-auto grid size-4 shrink-0 place-items-center rounded-full border", on && "border-foreground")}>
                    {on && <span className="size-2 rounded-full bg-foreground" />}
                  </span>
                </button>
              )
            })}
          </div>
          {q.freeText && (
            <Input value={free} onChange={(e) => setFree(e.target.value)} onKeyDown={onEnter} disabled={act.disabled} aria-label="Your answer"
              placeholder={q.options?.length ? "Or answer in your own words…" : "Your answer"} className="h-8 text-[13px]" />
          )}
        </div>
      )}
    </Shell>
  )
}

// ---- budget --------------------------------------------------------------
function BudgetCard({ i, onAct }: { i: Interrupt; onAct?: OnAct }) {
  const b = i.budget!
  const act = useAct(i, onAct)
  const off = act.disabled || act.submitting
  return (
    <Shell tone="wait" head={<>
      <StatusChip status="wait">Waiting on you</StatusChip>
      <span className="text-[13px] font-medium">Budget</span>
      <Id id={i.id} />
    </>} footer={<>
      {b.options.includes("grant") && <Button size="sm" variant="outline" disabled={off} onClick={() => act.run({ kind: "grant" })}><Plus className="size-3.5" />Grant more turns</Button>}
      {b.options.includes("finalize") && <Button size="sm" variant="outline" disabled={off} onClick={() => act.run({ kind: "finalize" })}><Flag className="size-3.5" />Finish now<span className="text-muted-foreground">· partial result</span></Button>}
      {b.options.includes("stop") && <Button size="sm" variant="ghost" disabled={off} onClick={() => act.run({ kind: "stop" })} className="ml-auto text-status-bad hover:text-status-bad"><Square className="size-3 fill-current" />Stop run</Button>}
    </>}>
      <div>
        <div className="text-[14px] font-medium">{i.title}</div>
        {i.detail && <p className="mt-0.5 text-[13px] text-muted-foreground">{i.detail}</p>}
      </div>
      {act.refusal && <Refusal {...act.refusal} />}
      <div className="space-y-1.5">
        <div className="flex items-baseline justify-between text-xs">
          <span className="text-muted-foreground">Turns used</span>
          <span className="font-mono tabular-nums">{b.used} <span className="text-muted-foreground">of {b.limit}</span></span>
        </div>
        <div className="h-1.5 overflow-hidden rounded-full bg-muted"><div className="h-full w-full rounded-full bg-status-wait" /></div>
      </div>
    </Shell>
  )
}

// ---- review --------------------------------------------------------------
function ReviewCard({ i, onOpenView, onAct }: { i: Interrupt; onOpenView: OpenView; onAct?: OnAct }) {
  const r = i.review!
  const act = useAct(i, onAct)
  const off = act.disabled || act.submitting
  return (
    <Shell tone="wait" head={<>
      <StatusChip status="wait">Waiting on you</StatusChip>
      <span className="text-[13px] font-medium">Review</span>
      <Id id={i.id} />
    </>} footer={<>
      {r.view && <Button {...opensView} size="sm" variant="outline" onClick={() => onOpenView(r.view!)}><GitCompareArrows className="size-3.5" />Open diff</Button>}
      <Button size="sm" variant="ghost" className="ml-auto" disabled={off} onClick={() => act.run({ kind: "reject-review" })}>Reject</Button>
      <Button size="sm" disabled={off} onClick={() => act.run({ kind: "accept-review" })}>Accept {r.to}</Button>
    </>}>
      <div className="text-[14px] font-medium">{i.title}</div>
      {act.refusal && <Refusal {...act.refusal} />}
      <div className="flex items-center gap-3 rounded-lg border bg-muted/30 px-3 py-2.5 text-[13px]">
        <GitCompareArrows className="size-4 text-muted-foreground" />
        <span className="font-mono">{r.artifact}</span>
        <span className="font-mono text-muted-foreground">{r.from} → {r.to}</span>
        <span className="ml-auto text-muted-foreground"><span className="font-medium text-foreground">{r.changes}</span> changed cells</span>
      </div>
      <p className="text-xs text-muted-foreground">Accepting makes {r.to} current. Rejecting keeps {r.from} and stops the rebuild here.</p>
    </Shell>
  )
}

// ---- approval: the exact action, signed ----------------------------------
function ApprovalCard({ i, onOpenView, onAct }: { i: Interrupt; onOpenView: OpenView; onAct?: OnAct }) {
  const a = i.action!
  const act = useAct(i, onAct)
  const [rejecting, setRejecting] = useState(false)
  const [note, setNote] = useState("")
  const s = act.submitting && (i.state === "open" || i.state === "refused") ? "submitting" : act.refusal && i.state === "open" ? "refused" : i.state
  const pending = s === "open" || s === "submitting" || s === "refused"
  const tone: Status = s === "granted" ? "ok" : pending ? "wait" : s === "rejected" ? "bad" : "idle"
  const chip = {
    open: <StatusChip status="wait">Needs your approval</StatusChip>, submitting: <StatusChip status="wait">Approving…</StatusChip>,
    refused: <StatusChip status="wait">Needs your approval</StatusChip>, granted: <StatusChip status="ok">Sent</StatusChip>,
    rejected: <StatusChip status="bad">Rejected</StatusChip>, "resolved-elsewhere": <StatusChip status="idle">Decided elsewhere</StatusChip>,
    voided: <StatusChip status="idle">Voided</StatusChip>, expired: <StatusChip status="idle">Expired</StatusChip>, resolved: <StatusChip status="ok">Done</StatusChip>,
  }[s]
  const elsewhere = i.decidedBy?.includes("another device") ? `Decided by ${i.decidedBy}` : `Decided on another device by ${i.decidedBy ?? "someone else"}`

  return (
    <Shell tone={tone} head={<>
      {chip}
      <span className={cn("text-[13px] font-medium", (s === "voided" || s === "expired") && "text-muted-foreground line-through")}>{i.title}</span>
      <Id id={i.id} extra={pending && a.expiresIn ? `expires in ${a.expiresIn}` : undefined} />
    </>} footer={
      pending && rejecting ? <>
        <Input value={note} onChange={(e) => setNote(e.target.value)} placeholder="Why (optional, the agent sees it)" aria-label="Rejection note"
          className="h-8 min-w-0 flex-1 text-[13px]" />
        <Button size="sm" variant="ghost" onClick={() => setRejecting(false)}>Cancel</Button>
        <Button size="sm" variant="outline" className="text-status-bad" disabled={act.disabled || s === "submitting"}
          onClick={() => act.run({ kind: "reject", note: note.trim() || undefined })}>Reject</Button>
      </> : pending ? <>
        <Button size="sm" variant="ghost" disabled={act.disabled || s === "submitting"} onClick={() => setRejecting(true)}>Reject</Button>
        <Button size="sm" variant="outline" disabled title="Editing an action before approval arrives with the session store"><Pencil className="size-3.5" />Edit first</Button>
        <Button size="sm" className="ml-auto" disabled={act.disabled || s === "submitting"} autoFocus={false}
          onClick={() => act.run({ kind: "approve", signature: a.signature })}>
          {s === "submitting" && <Spinner className="size-3.5" />}Approve and send
        </Button>
      </> : s === "expired" ? <>
        <span className="text-xs text-muted-foreground">Nothing was sent. The agent can ask again with a fresh signature.</span>
        <Button size="sm" variant="outline" className="ml-auto" disabled><RotateCcw className="size-3.5" />Ask again</Button>
      </> : undefined
    }>
      {s === "granted" && <div className="flex items-center gap-2 text-[13px]"><Glyph status="ok" />
        <span>Approved by {i.decidedBy ?? "you"} and executed exactly as shown{a.delivered ? `: ${a.delivered}` : ""}.</span></div>}
      {s === "resolved-elsewhere" && <div className="flex items-center gap-2 text-[13px]"><Glyph status="idle" />
        <span>{elsewhere}. This card is now read-only.</span></div>}
      {s === "voided" && <div className="text-[13px] text-muted-foreground">{i.detail ?? "This request no longer applies: the run ended before a decision."} Nothing was sent.</div>}
      {s === "rejected" && <div className="text-[13px] text-muted-foreground">Rejected by {i.decidedBy ?? "you"}. Nothing was sent; the agent was told.</div>}
      {s === "refused" && act.refusal && <Refusal {...act.refusal} />}

      <div className={cn("space-y-3", !pending && "opacity-80")}>
        <div className="flex flex-wrap items-center gap-2">
          <code className="rounded-md border bg-muted px-1.5 py-0.5 font-mono text-[12.5px] font-medium">{a.tool}</code>
          {a.leavesSystem && <LeavesBadge />}
          {pending && <span className="text-xs text-muted-foreground">The agent runs exactly this tool with exactly these values.</span>}
        </div>
        <dl className="grid grid-cols-[92px_1fr] gap-x-3 gap-y-1.5 text-[13px]">
          {a.facts.map(([k, v]) => (
            <div key={k} className="contents">
              <dt className="text-muted-foreground">{k}</dt><dd className="min-w-0 font-mono text-[12.5px] break-all">{v}</dd>
            </div>
          ))}
          {a.targets && a.targets.length > 0 && (
            <div className="contents">
              <dt className="text-muted-foreground">acts on</dt>
              <dd className="min-w-0 font-mono text-[12.5px] break-all">{a.targets.join(", ")}</dd>
            </div>
          )}
        </dl>
        {a.payloadPreview && (pending || s === "granted") && (
          <div className="rounded-lg border bg-muted/40 px-3 py-2.5">
            <div className="mb-1 flex items-center justify-between">
              <span className="text-[11.5px] font-medium tracking-wide text-muted-foreground uppercase">Body</span>
              {a.payloadView && <button {...opensView} onClick={() => onOpenView(a.payloadView!)}
                className="inline-flex items-center gap-1 text-xs font-medium text-foreground underline-offset-2 hover:underline">
                View full payload<ArrowUpRight className="size-3" /></button>}
            </div>
            <p className="line-clamp-2 text-[13px] leading-5">{a.payloadPreview}</p>
          </div>
        )}
        {pending && (
          <div className="flex items-center gap-2 text-xs text-muted-foreground">
            <FileSignature className="size-3.5" />
            <span className="shrink-0">You approve exactly this. Any change voids it.</span>
            <span className="ml-auto min-w-0 font-mono break-all" aria-label="Signature">{a.signature}</span>
          </div>
        )}
      </div>
    </Shell>
  )
}

/** `onAct` absent means read only (a viewer, or an admin reading someone else's session). */
export function InterruptCard({ i, onOpenView, onAct }: { i: Interrupt; onOpenView: OpenView; onAct?: OnAct }) {
  return <div data-interrupt-kind={i.kind} data-interrupt-state={i.state} data-interrupt-id={i.id}><InterruptBody i={i} onOpenView={onOpenView} onAct={onAct} /></div>
}

function InterruptBody({ i, onOpenView, onAct }: { i: Interrupt; onOpenView: OpenView; onAct?: OnAct }) {
  if (i.kind === "approval") return <ApprovalCard i={i} onOpenView={onOpenView} onAct={onAct} />
  const closed = closedStates(i)
  if (closed) return closed
  if (i.kind === "budget") return <BudgetCard i={i} onAct={onAct} />
  if (i.kind === "review") return <ReviewCard i={i} onOpenView={onOpenView} onAct={onAct} />
  return <QuestionCard i={i} onAct={onAct} />
}
