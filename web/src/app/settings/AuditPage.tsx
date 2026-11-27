import { useState } from "react"
import { ChevronLeft, ChevronRight, ScrollText } from "lucide-react"
import { useAudit, useMembers, type AuditEntry } from "@/api"
import { cn } from "@/lib/utils"
import { Button } from "@/components/ui/button"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Spinner } from "@/components/ui/spinner"
import { ErrorNote } from "@/app/shell/ErrorNote"
import { PageBody, PageEmpty, PageHeader, Person, td, th, tr } from "@/app/pages/common"
import { canManageOrg, initials, useViewer } from "@/features/auth/principal"
import { SettingsFrame } from "./SettingsPage"

/** The audit actions the server writes. */
export const AUDIT_ACTIONS = [
  "sign_in", "sign_in_failed", "signup", "claim", "invite_sent", "invite_revoked", "invite_accepted", "role_changed",
  "user_deactivated", "token_created", "token_revoked", "read_session_as_admin", "stop_run_as_admin", "guest_created", "guest_saved",
] as const

const tone: Record<string, string> = {
  read_session_as_admin: "border-status-wait/40 bg-status-wait-soft text-status-wait",
  stop_run_as_admin: "border-status-wait/40 bg-status-wait-soft text-status-wait",
  role_changed: "border-status-run/30 bg-status-run-soft text-status-run",
  invite_revoked: "border-status-bad/30 bg-status-bad-soft text-status-bad",
  user_deactivated: "border-status-bad/30 bg-status-bad-soft text-status-bad",
  token_revoked: "border-status-bad/30 bg-status-bad-soft text-status-bad",
  sign_in_failed: "border-status-bad/30 bg-status-bad-soft text-status-bad",
}

const PAGE = 50

function detailText(d: unknown): string {
  if (d == null || d === "") return ""
  if (typeof d === "string") return d
  if (typeof d === "object") return Object.entries(d as Record<string, unknown>).map(([k, v]) => `${k} ${typeof v === "object" ? JSON.stringify(v) : String(v)}`).join(" · ")
  return String(d)
}

export function AuditPage() {
  const viewer = useViewer()
  const manager = canManageOrg(viewer.role)
  const [action, setAction] = useState("all")
  const [actor, setActor] = useState("all")
  const [cursors, setCursors] = useState<(string | undefined)[]>([undefined])
  const before = cursors.at(-1)
  const query = { limit: PAGE, before, action: action === "all" ? undefined : action, actor: actor === "all" ? undefined : actor }
  const audit = useAudit(query, manager)
  const members = useMembers(manager)
  const resetPaging = () => setCursors([undefined])
  const rows: AuditEntry[] = audit.data?.entries ?? []
  const nameOf = (e: AuditEntry) => {
    const m = e.actor ? members.data?.find((x) => x.id === e.actor) : undefined
    return m?.name || m?.email || e.actor || "system"
  }

  return (
    <SettingsFrame active="audit">
      <PageHeader title="Audit log" sub="Append-only record of sign-ins, access and admin changes" />
      <PageBody>
        {!manager ? <PageEmpty icon={ScrollText} title="Only owners and admins read the audit log" body="Ask an admin if you need to know who did what." /> : <>
          <div className="mb-3 flex flex-wrap items-center gap-2">
            <Select value={actor} onValueChange={(v) => { setActor(v); resetPaging() }}>
              <SelectTrigger className="h-8 w-48 text-[13px]" aria-label="Filter by actor"><span className="text-muted-foreground">Actor</span><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="all">Anyone</SelectItem>
                {(members.data ?? []).map((m) => <SelectItem key={m.id} value={m.id}>{m.name || m.email}</SelectItem>)}
              </SelectContent>
            </Select>
            <Select value={action} onValueChange={(v) => { setAction(v); resetPaging() }}>
              <SelectTrigger className="h-8 w-56 text-[13px]" aria-label="Filter by action"><span className="text-muted-foreground">Action</span><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="all">All actions</SelectItem>
                {AUDIT_ACTIONS.map((a) => <SelectItem key={a} value={a}><span className="font-mono text-[12px]">{a}</span></SelectItem>)}
              </SelectContent>
            </Select>
            {audit.isFetching && <Spinner className="size-3.5 text-muted-foreground" />}
            <span className="ml-auto text-[12px] text-muted-foreground">{rows.length} entries on this page · local time</span>
          </div>
          <ErrorNote error={audit.error} className="mb-3" />
          {audit.isPending ? <div className="flex justify-center py-8"><Spinner className="size-5 text-muted-foreground" /></div>
            : rows.length === 0 ? <PageEmpty icon={ScrollText} title="No entries" body="Nothing matches these filters." /> : (
              <div className="overflow-x-auto rounded-lg border bg-card">
                <table className="w-full">
                  <thead className="border-b bg-muted/30"><tr>
                    <th className={cn(th, "w-44")}>Time</th><th className={th}>Actor</th><th className={th}>Action</th><th className={th}>Target</th><th className={cn(th, "text-right")}>IP</th>
                  </tr></thead>
                  <tbody>
                    {rows.map((e, i) => {
                      const who = nameOf(e)
                      const d = new Date(e.at)
                      return (
                        <tr key={e.id ?? `${e.at}-${i}`} className={tr}>
                          <td className={cn(td, "h-10 whitespace-nowrap font-mono text-[12px] tabular-nums")}>{Number.isNaN(d.getTime()) ? e.at : d.toLocaleString()}</td>
                          <td className={cn(td, "h-10")}><Person name={who} initials={initials(who)} /></td>
                          <td className={cn(td, "h-10")}>
                            <span className={cn("inline-flex h-5 items-center rounded border px-1.5 font-mono text-[11.5px]", tone[e.action] ?? "bg-muted/60 text-foreground/80")}>{e.action}</span>
                          </td>
                          <td className={cn(td, "h-10")}>
                            <div className="flex min-w-0 items-baseline gap-2">
                              <span className="whitespace-nowrap">{e.target ?? ""}</span>
                              <span className="truncate text-[12.5px] text-muted-foreground">{detailText(e.detail)}</span>
                            </div>
                          </td>
                          <td className={cn(td, "h-10 text-right font-mono text-[12px] text-muted-foreground tabular-nums")}>{e.ip ?? ""}</td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>
            )}
          <div className="mt-3 flex items-center gap-2 text-[12px] text-muted-foreground">
            <span>Reads of someone else's session are always recorded, including by owners.</span>
            <Button size="icon-xs" variant="outline" className="ml-auto" disabled={cursors.length < 2} aria-label="Newer entries"
              onClick={() => setCursors((c) => c.slice(0, -1))}><ChevronLeft /></Button>
            <Button size="icon-xs" variant="outline" disabled={!audit.data?.next && rows.length < PAGE} aria-label="Older entries"
              onClick={() => {
                const last = rows.at(-1)
                const nextCursor = audit.data?.next ?? (last?.id != null ? String(last.id) : last?.at)
                if (nextCursor) setCursors((c) => [...c, nextCursor])
              }}><ChevronRight /></Button>
          </div>
        </>}
      </PageBody>
    </SettingsFrame>
  )
}
