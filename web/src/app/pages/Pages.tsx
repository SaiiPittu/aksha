import { useMemo, useRef, useState } from "react"
import { Link } from "react-router"
import { Archive, ArchiveRestore, ArrowUpRight, FlaskConical, FolderOpen, Inbox, LayoutList } from "lucide-react"
import { useArchiveSession, useArtifacts, useDocuments, useSessionStatus, type ArtifactRow, type DocumentRow, type Waiting } from "@/api"
import { Button } from "@/components/ui/button"
import { Spinner } from "@/components/ui/spinner"
import { StatusChip } from "@/app/shell/Status"
import { ErrorNote } from "@/app/shell/ErrorNote"
import { ViewPanel } from "@/app/panel/ViewPanel"
import { FreshnessChip } from "@/app/views/LineageView"
import { useSessionList } from "@/features/session"
import { artifactView, kindForMedia } from "@/features/session/reducer"
import type { PanelState, SessionSummary, Status, ViewRef } from "@/features/session/types"
import { canCommand, useViewer } from "@/features/auth/principal"
import { FilterTabs, PageBody, PageEmpty, PageHeader, fmtWhen, td, th, tr } from "./common"

const sessionLink = (id: string) => `/s/${encodeURIComponent(id)}`

// ---- inbox: one row per pending item ------------------------------------------------

/** The exact one-line action a pending item asks for. */
export function waitingLine(w: Waiting): { what: string; action: string } {
  const facts = Array.isArray(w.facts) ? w.facts : Object.entries(w.facts ?? {})
  const factText = facts.map(([k, v]) => `${k} ${typeof v === "string" ? v : JSON.stringify(v)}`).join(", ")
  const q = (w.questions ?? []).map((x) => (Array.isArray(x) ? x[1] : x.text)).filter(Boolean)
  switch (w.kind) {
    case "approval": return { what: "Approval", action: `Approve ${w.tool ?? "the action"}${factText ? `: ${factText}` : ""}` }
    case "choice": return { what: "Choice", action: `${q[0] ?? w.label ?? "Choose one"}${w.options?.length ? ` (${w.options.join(", ")})` : ""}` }
    case "route": return { what: "Route", action: `Pick an agent${w.options?.length ? `: ${w.options.join(", ")}` : ""}` }
    case "budget": return { what: "Budget", action: `Grant more budget or finish now${w.label ? ` (${w.label})` : ""}` }
    case "review": return { what: "Review", action: `Review the rebuilt result${w.label ? `: ${w.label}` : ""}` }
    default: return { what: "Question", action: q.join(" · ") || w.label || "Answer the agent's question" }
  }
}

function InboxRows({ s }: { s: SessionSummary }) {
  const q = useSessionStatus(s.id)
  const items = q.data?.waiting_on ?? []
  if (q.isLoading) return <tr className={tr}><td className={td} colSpan={4}><Spinner className="size-3.5 text-muted-foreground" /></td></tr>
  // The list knows one pending item even when the status read fails.
  const rows: Waiting[] = items.length ? items : s.waiting ? [{ kind: s.waiting.kind, interrupt_id: s.waiting.id }] : []
  return <>{rows.map((w, n) => {
    const line = waitingLine(w)
    return (
      <tr key={`${s.id}-${w.interrupt_id ?? w.approval_id ?? n}`} className={tr}>
        <td className={td}><StatusChip status="wait">{line.what}</StatusChip></td>
        <td className={`${td} max-w-0 w-full`}>
          <div className="truncate font-medium" title={line.action}>{line.action}</div>
          <div className="truncate text-[12px] text-muted-foreground">{s.title} · {s.agent}</div>
        </td>
        <td className={`${td} font-mono text-[12px] whitespace-nowrap text-muted-foreground`}>{w.approval_id ?? w.interrupt_id}</td>
        <td className={`${td} text-right`}><Button size="sm" variant="outline" asChild><Link to={sessionLink(s.id)}>Open<ArrowUpRight className="size-3" /></Link></Button></td>
      </tr>
    )
  })}</>
}

export function InboxPage() {
  const { sessions, loading, error } = useSessionList()
  const waiting = sessions.filter((s) => s.status === "wait" || s.waiting)
  return <>
    <PageHeader title="Waiting on you" sub="Approvals, questions, reviews and budgets across your sessions" />
    <PageBody>
      {error ? <ErrorNote error={error} /> : null}
      {loading ? <Spinner className="size-4 text-muted-foreground" /> : waiting.length === 0
        ? <PageEmpty icon={Inbox} title="Nothing is waiting on you" body="When an agent asks a question or needs an approval, it shows up here as well as in its session." />
        : (
          <div className="overflow-hidden rounded-lg border">
            <table className="w-full">
              <thead className="border-b bg-muted/40"><tr><th className={th}>Kind</th><th className={th}>What it asks</th><th className={th}>ID</th><th className={th} /></tr></thead>
              <tbody>{waiting.map((s) => <InboxRows key={s.id} s={s} />)}</tbody>
            </table>
          </div>
        )}
    </PageBody>
  </>
}

// ---- sessions ------------------------------------------------------------------------

type SessionFilter = "all" | "wait" | "run" | "ok" | "bad" | "idle" | "archived"

export function SessionsPage() {
  const [filter, setFilter] = useState<SessionFilter>("all")
  const live = useSessionList(false)
  const archived = useSessionList(true)
  const archive = useArchiveSession()
  const viewer = useViewer()
  const can = canCommand(viewer.role)
  const list = filter === "archived" ? archived.sessions : live.sessions
  const count = (st: Status) => live.sessions.filter((s) => s.status === st).length
  const shown = filter === "all" || filter === "archived" ? list : list.filter((s) => s.status === filter)
  const loading = filter === "archived" ? archived.loading : live.loading
  const error = (filter === "archived" ? archived.error : live.error) ?? archive.error
  return <>
    <PageHeader title="Sessions" sub="Every session in this workspace, with filters and archive" />
    <PageBody>
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <FilterTabs<SessionFilter> value={filter} onChange={setFilter} items={[
          { key: "all", label: "All", count: live.sessions.length },
          { key: "wait", label: "Waiting", count: count("wait"), tone: "text-status-wait" },
          { key: "run", label: "Running", count: count("run") },
          { key: "ok", label: "Done", count: count("ok") },
          { key: "bad", label: "Failed", count: count("bad") },
          { key: "idle", label: "Stopped", count: count("idle") },
          { key: "archived", label: "Archived", count: archived.sessions.length },
        ]} />
      </div>
      {error ? <ErrorNote error={error} /> : null}
      {loading ? <Spinner className="size-4 text-muted-foreground" /> : shown.length === 0
        ? <PageEmpty icon={LayoutList} title={filter === "archived" ? "No archived sessions" : "No sessions to show"} body="Start a new chat; its session appears here and in the left rail." />
        : (
          <div className="overflow-hidden rounded-lg border">
            <table className="w-full">
              <thead className="border-b bg-muted/40"><tr>
                <th className={th}>Session</th><th className={th}>Agent</th><th className={th}>Status</th><th className={th}>Last activity</th><th className={th} />
              </tr></thead>
              <tbody>
                {shown.map((s) => (
                  <tr key={s.id} className={tr}>
                    <td className={`${td} max-w-0 w-full`}>
                      <Link to={sessionLink(s.id)} className="block truncate font-medium hover:underline">{s.title}</Link>
                      <span className="font-mono text-[11.5px] text-muted-foreground">{s.id}{s.run && ` · ${s.run}`}</span>
                    </td>
                    <td className={`${td} whitespace-nowrap`}>{s.agent}</td>
                    <td className={td}><StatusChip status={s.status}>{s.statusLabel}</StatusChip></td>
                    <td className={`${td} whitespace-nowrap text-muted-foreground`}>{fmtWhen(s.lastAt)}</td>
                    <td className={`${td} text-right`}>
                      {can && (
                        <Button size="sm" variant="ghost" className="text-muted-foreground" disabled={archive.isPending}
                          onClick={() => archive.mutate({ session: s.id, archive: !s.archived })}>
                          {s.archived ? <><ArchiveRestore className="size-3.5" />Restore</> : <><Archive className="size-3.5" />Archive</>}
                        </Button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
    </PageBody>
  </>
}

// ---- files and artifacts ----------------------------------------------------------------

export function documentRowView(d: DocumentRow): ViewRef {
  const versions = d.versions.length ? d.versions : Array.from({ length: d.version }, (_, i) => i + 1)
  return {
    id: `input:${d.path}@v${d.version}`, kind: kindForMedia(d.media_type ?? undefined, d.path), title: d.path, source: "input",
    version: `v${d.version}`, versions: versions.map((v) => `v${v}`), docKey: d.doc_key, mediaType: d.media_type ?? undefined,
    freshness: (d.freshness ?? undefined) as ViewRef["freshness"],
  }
}

export function artifactRowView(a: ArtifactRow): ViewRef {
  return artifactView(a.ref, {
    versions: a.versions.length ? a.versions.map((v) => `v${v}`) : undefined,
    mediaType: a.media_type ?? undefined, freshness: (a.freshness ?? undefined) as ViewRef["freshness"],
  })
}

const fmtSize = (n?: number | null) => (n == null ? "" : n < 1024 ? `${n} B` : n < 1024 ** 2 ? `${(n / 1024).toFixed(1)} KB` : `${(n / 1024 ** 2).toFixed(1)} MB`)

export function FilesPage() {
  const docs = useDocuments()
  const arts = useArtifacts()
  const [tab, setTab] = useState<"artifacts" | "inputs">("artifacts")
  const [panel, setPanel] = useState<PanelState>({ mode: "closed", tabs: [] })
  const area = useRef<HTMLDivElement>(null)
  const open = (v: ViewRef) => setPanel((p) => ({ mode: p.mode === "closed" ? "pinned" : p.mode, tabs: p.tabs.some((t) => t.id === v.id) ? p.tabs : [...p.tabs, v], active: v.id }))
  const artifacts = useMemo(() => arts.data ?? [], [arts.data])
  const documents = useMemo(() => docs.data ?? [], [docs.data])
  const stale = artifacts.filter((a) => a.freshness && a.freshness !== "current" && a.freshness !== "current_by_cutoff").length
  const q = tab === "artifacts" ? arts : docs
  return <>
    <PageHeader title="Files and artifacts" sub="Inputs you uploaded and artifacts agents saved, with their versions and freshness" />
    <div ref={area} className="relative flex min-h-0 flex-1">
      <PageBody className="min-w-0">
        <div className="mb-4 flex flex-wrap items-center gap-3">
          <FilterTabs value={tab} onChange={setTab} items={[
            { key: "artifacts", label: "Artifacts", count: artifacts.length },
            { key: "inputs", label: "Inputs", count: documents.length },
          ]} />
          {stale > 0 && <span className="text-[12.5px] text-status-wait">{stale} artifact{stale === 1 ? "" : "s"} out of date</span>}
        </div>
        {q.error ? <ErrorNote error={q.error} /> : null}
        {q.isLoading ? <Spinner className="size-4 text-muted-foreground" />
          : tab === "artifacts" ? (artifacts.length === 0
            ? <PageEmpty icon={FolderOpen} title="No artifacts yet" body="Artifacts an agent saves are listed here, and open in the view panel." />
            : (
              <div className="overflow-hidden rounded-lg border">
                <table className="w-full">
                  <thead className="border-b bg-muted/40"><tr>
                    <th className={th}>Artifact</th><th className={th}>Version</th><th className={th}>Freshness</th><th className={th}>Session</th><th className={th}>Saved</th>
                  </tr></thead>
                  <tbody>
                    {artifacts.map((a) => (
                      <tr key={a.ref} className={`${tr} cursor-pointer`} onClick={() => open(artifactRowView(a))}>
                        <td className={`${td} max-w-0 w-full`}><span className="block truncate font-mono text-[12.5px]">{a.key ? `${a.kind}/${a.key}` : a.kind}</span></td>
                        <td className={`${td} font-mono text-[12px] whitespace-nowrap`}>v{a.version}{a.versions.length > 1 && <span className="text-muted-foreground"> of {a.versions.length}</span>}</td>
                        <td className={td}><FreshnessChip f={a.freshness ?? "current"} /></td>
                        <td className={`${td} whitespace-nowrap`}>{a.session
                          ? <Link onClick={(e) => e.stopPropagation()} to={sessionLink(a.session)} className="font-mono text-[12px] hover:underline">{a.session}</Link>
                          : <span className="text-muted-foreground">rebuild</span>}</td>
                        <td className={`${td} whitespace-nowrap text-muted-foreground`}>{fmtWhen(a.saved_at)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ))
            : (documents.length === 0
              ? <PageEmpty icon={FolderOpen} title="No inputs yet" body="Files you attach in a chat are listed here, and open in the view panel." />
              : (
                <div className="overflow-hidden rounded-lg border">
                  <table className="w-full">
                    <thead className="border-b bg-muted/40"><tr>
                      <th className={th}>Path</th><th className={th}>Version</th><th className={th}>Type</th><th className={th}>Size</th><th className={th}>Stored</th>
                    </tr></thead>
                    <tbody>
                      {documents.map((d) => (
                        <tr key={d.doc_key} className={`${tr} cursor-pointer`} onClick={() => open(documentRowView(d))}>
                          <td className={`${td} max-w-0 w-full`}><span className="block truncate font-mono text-[12.5px]">{d.path}</span></td>
                          <td className={`${td} font-mono text-[12px] whitespace-nowrap`}>v{d.version}</td>
                          <td className={`${td} whitespace-nowrap text-muted-foreground`}>{d.media_type}</td>
                          <td className={`${td} whitespace-nowrap text-muted-foreground tabular-nums`}>{fmtSize(d.size)}</td>
                          <td className={`${td} whitespace-nowrap text-muted-foreground`}>{fmtWhen(d.stored_at)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ))}
      </PageBody>
      <ViewPanel panel={panel} onChange={setPanel} onClose={() => setPanel((p) => ({ ...p, mode: "closed" }))} />
    </div>
  </>
}

export function EvalsPage() {
  return <>
    <PageHeader title="Evals" sub="Eval runs and their scores" />
    <PageBody><PageEmpty icon={FlaskConical} title="No eval runs yet" body="Run an eval suite from the terminal with `aksha eval`; its runs show up here." /></PageBody>
  </>
}
