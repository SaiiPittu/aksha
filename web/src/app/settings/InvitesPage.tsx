import { useState } from "react"
import { RotateCw, Send, X } from "lucide-react"
import { inviteLink, useCreateInvites, useInvites, useResendInvite, useRevokeInvite, type Invite, type Role } from "@/api"
import { cn } from "@/lib/utils"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Spinner } from "@/components/ui/spinner"
import { ErrorNote } from "@/app/shell/ErrorNote"
import { ConfirmDialog } from "@/app/shell/ConfirmDialog"
import { CopyField } from "@/app/shell/CopyField"
import { PageBody, PageEmpty, PageHeader, fmtWhen, roleLabel, td, th, tr } from "@/app/pages/common"
import { canManageOrg, useViewer } from "@/features/auth/principal"
import { emailError } from "@/features/auth/validate"
import { SettingsFrame } from "./SettingsPage"

const INVITABLE: Role[] = ["admin", "member", "viewer"]

const stateTone = (s: string) =>
  s === "valid" ? "border-status-wait/40 text-status-wait" : s === "used" ? "border-status-ok/40 text-status-ok" : "text-muted-foreground"

export function InvitesPage() {
  const viewer = useViewer()
  const guest = viewer.mode === "guest"
  const manager = canManageOrg(viewer.role) && !guest
  return (
    <SettingsFrame active="invites">
      <PageHeader title="Invites" sub="Aksha does not send email: copy each link and share it yourself. Links expire in 7 days and work once." />
      <PageBody className="flex flex-col gap-5">
        {manager ? <><InviteForm /><InviteList /></>
          : guest ? <PageEmpty icon={Send} title="A guest invites nobody" body="Save the sandbox as an account first. It stays your own workspace." />
          : <PageEmpty icon={Send} title="Only owners and admins invite people" body="Ask an admin of your organisation for an invite link." />}
      </PageBody>
    </SettingsFrame>
  )
}

/** Shown right after creating or resending: the links to copy, once in view. */
function Links({ invites }: { invites: Invite[] }) {
  return (
    <div className="flex flex-col gap-2 rounded-md border border-status-ok/30 bg-status-ok-soft/50 p-3">
      <p className="text-[12.5px] font-medium">Copy these links and send them to each person</p>
      {invites.map((i) => {
        const link = inviteLink(i)
        return (
          <div key={i.id} className="grid items-center gap-2 sm:grid-cols-[200px_1fr]">
            <span className="truncate text-[13px]">{i.email} <span className="text-muted-foreground">· {roleLabel[i.role]}</span></span>
            {link ? <CopyField value={link} label={`invite link for ${i.email}`} /> : <span className="text-[12.5px] text-muted-foreground">The server returned no link.</span>}
          </div>
        )
      })}
    </div>
  )
}

function InviteForm() {
  const create = useCreateInvites()
  const [emails, setEmails] = useState<string[]>([])
  const [draft, setDraft] = useState("")
  const [role, setRole] = useState<Role>("member")
  const [error, setError] = useState<string>()

  const addDraft = (): string[] | null => {
    const parts = draft.split(/[\s,;]+/).map((x) => x.trim()).filter(Boolean)
    const bad = parts.find((p) => emailError(p))
    if (bad) { setError(`"${bad}" is not an email address.`); return null }
    setError(undefined)
    const next = [...emails, ...parts.filter((p) => !emails.includes(p))]
    setEmails(next); setDraft("")
    return next
  }
  const submit = (e: React.FormEvent) => {
    e.preventDefault()
    const list = draft.trim() ? addDraft() : emails
    if (!list) return
    if (!list.length) { setError("Add at least one email."); return }
    create.mutate({ emails: list, role }, { onSuccess: () => setEmails([]) })
  }

  return (
    <form onSubmit={submit} noValidate aria-label="Invite people" className="flex flex-col gap-3 rounded-lg border bg-card p-4">
      <div>
        <h2 className="text-[13.5px] font-semibold">Invite people</h2>
        <p className="text-[12.5px] text-muted-foreground">Each person gets their own one-time link.</p>
      </div>
      <label className="flex min-h-16 flex-wrap content-start items-start gap-1.5 rounded-md border bg-background p-1.5 focus-within:ring-2 focus-within:ring-ring/40 dark:bg-input/30">
        <span className="sr-only">Emails</span>
        {emails.map((e) => (
          <span key={e} className="inline-flex h-6 items-center gap-1 rounded-md bg-muted pr-1 pl-2 text-[12.5px]">
            {e}<button type="button" aria-label={`Remove ${e}`} onClick={() => setEmails(emails.filter((x) => x !== e))} className="text-muted-foreground hover:text-foreground"><X className="size-3" /></button>
          </span>
        ))}
        <input value={draft} onChange={(e) => setDraft(e.target.value)} placeholder="Add emails, comma separated" aria-label="Add emails"
          onKeyDown={(e) => { if ((e.key === "Enter" || e.key === ",") && draft.trim()) { e.preventDefault(); addDraft() } }}
          onBlur={() => { if (draft.trim()) addDraft() }}
          className="h-6 min-w-40 flex-1 bg-transparent px-1 text-[13px] outline-none placeholder:text-muted-foreground" />
      </label>
      {error && <p role="alert" className="text-[12.5px] text-status-bad">{error}</p>}
      <ErrorNote error={create.error} />
      <div className="flex items-center gap-2">
        <span className="text-[12.5px] text-muted-foreground">Role</span>
        <Select value={role} onValueChange={(v) => setRole(v as Role)}>
          <SelectTrigger size="sm" className="w-32 text-[13px]" aria-label="Role for new invites"><SelectValue /></SelectTrigger>
          <SelectContent>{INVITABLE.map((r) => <SelectItem key={r} value={r}>{roleLabel[r]}</SelectItem>)}</SelectContent>
        </Select>
        <Button size="sm" type="submit" className="ml-auto" disabled={create.isPending}>
          {create.isPending ? <Spinner className="size-3.5" /> : <Send />}Create {emails.length > 1 ? `${emails.length} invites` : "invite"}
        </Button>
      </div>
      {create.data && create.data.length > 0 && <Links invites={create.data} />}
    </form>
  )
}

function InviteList() {
  const invites = useInvites()
  const resend = useResendInvite()
  const revoke = useRevokeInvite()
  const [fresh, setFresh] = useState<Invite[]>([])
  const [revoking, setRevoking] = useState<Invite | null>(null)
  const rows = invites.data ?? []
  return (
    <section className="flex flex-col gap-3">
      <h2 className="text-[13.5px] font-semibold">Pending and past invites</h2>
      <ErrorNote error={invites.error} />
      <ErrorNote error={resend.error} />
      <ErrorNote error={revoke.error} />
      {fresh.length > 0 && <Links invites={fresh} />}
      {invites.isPending ? <div className="flex justify-center py-6"><Spinner className="size-5 text-muted-foreground" /></div>
        : rows.length === 0 ? <p className="text-[13px] text-muted-foreground">No invites yet.</p> : (
          <div className="overflow-x-auto rounded-lg border bg-card">
            <table className="w-full">
              <thead className="border-b bg-muted/30"><tr>
                <th className={th}>Email</th><th className={th}>Role</th><th className={th}>State</th><th className={th}>Expires</th><th className={th}>Invited by</th>
                <th className={cn(th, "w-44")}><span className="sr-only">Actions</span></th>
              </tr></thead>
              <tbody>
                {rows.map((i) => (
                  <tr key={i.id} className={tr}>
                    <td className={td}>{i.email}</td>
                    <td className={td}>{roleLabel[i.role]}</td>
                    <td className={td}><Badge variant="outline" className={stateTone(i.state)}>{i.state}</Badge></td>
                    <td className={cn(td, "whitespace-nowrap text-muted-foreground")}>{i.expires_at ? new Date(i.expires_at).toLocaleDateString() : "—"}</td>
                    <td className={cn(td, "text-muted-foreground")}>{i.invited_by || "—"} {i.created_at && <span>· {fmtWhen(i.created_at)}</span>}</td>
                    <td className={cn(td, "py-0")}>
                      {(i.state === "valid" || i.state === "expired") && (
                        <div className="flex justify-end gap-1">
                          <Button size="xs" variant="outline" disabled={resend.isPending}
                            onClick={() => resend.mutate(i.id, { onSuccess: (inv) => setFresh((f) => [inv, ...f.filter((x) => x.id !== inv.id)]) })}>
                            <RotateCw />Resend</Button>
                          {i.state === "valid" && <Button size="xs" variant="ghost" className="text-status-bad hover:text-status-bad" onClick={() => setRevoking(i)}>Revoke</Button>}
                        </div>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      <ConfirmDialog open={!!revoking} destructive title="Revoke this invite?" confirm="Revoke"
        body={`The link for ${revoking?.email ?? ""} stops working. You can invite them again later.`}
        onConfirm={() => { if (revoking) revoke.mutate(revoking.id); setRevoking(null) }} onCancel={() => setRevoking(null)} />
    </section>
  )
}
