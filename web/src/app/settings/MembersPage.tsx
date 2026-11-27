import { useState } from "react"
import { Check, Crown, Eye, ShieldCheck, User, X } from "lucide-react"
import { useMembers, useUpdateMember, type Member, type Role } from "@/api"
import { cn } from "@/lib/utils"
import { Button } from "@/components/ui/button"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Spinner } from "@/components/ui/spinner"
import { StatusChip } from "@/app/shell/Status"
import { ErrorNote } from "@/app/shell/ErrorNote"
import { ConfirmDialog } from "@/app/shell/ConfirmDialog"
import { PageBody, PageHeader, Person, fmtWhen, roleLabel, td, th, tr } from "@/app/pages/common"
import { assignableRoles, canDeactivate, canManageOrg, initials, useViewer } from "@/features/auth/principal"
import { SettingsFrame } from "./SettingsPage"

const roles: { role: Role; icon: React.ElementType; can: string[] }[] = [
  { role: "owner", icon: Crown, can: ["Everything an admin can", "Transfer ownership"] },
  { role: "admin", icon: ShieldCheck, can: ["Invite people, change roles", "Open anyone's session read-only (audited)", "Read the audit log"] },
  { role: "member", icon: User, can: ["Start sessions and run agents", "Approve actions in own sessions"] },
  { role: "viewer", icon: Eye, can: ["Read sessions shared with them", "Cannot run agents", "Cannot approve or answer"] },
]

type Pending = { kind: "deactivate" | "reactivate" | "transfer"; member: Member } | null

export function MembersPage() {
  const viewer = useViewer()
  const manager = canManageOrg(viewer.role)
  const members = useMembers()
  const update = useUpdateMember()
  const [confirm, setConfirm] = useState<Pending>(null)
  const rows = members.data ?? []
  const active = rows.filter((r) => r.active).length

  const setRole = (m: Member, role: Role) => {
    if (role === m.role) return
    if (role === "owner") { setConfirm({ kind: "transfer", member: m }); return }
    update.mutate({ user: m.id, role })
  }
  const doConfirm = () => {
    if (!confirm) return
    const { kind, member } = confirm
    setConfirm(null)
    if (kind === "transfer") update.mutate({ user: member.id, role: "owner" })
    else update.mutate({ user: member.id, active: kind === "reactivate" })
  }

  return (
    <SettingsFrame active="members">
      <PageHeader title="Members" sub={members.data ? `${active} active · roles decide what each person sees and can do` : "Roles decide what each person sees and can do"} />
      <PageBody>
        <section className="mb-5 rounded-lg border bg-card p-4">
          <h2 className="mb-2.5 text-[13.5px] font-semibold">What each role can do</h2>
          <div className="grid gap-x-5 gap-y-3 sm:grid-cols-2 lg:grid-cols-4">
            {roles.map(({ role, icon: Icon, can }) => (
              <div key={role}>
                <div className="mb-0.5 flex items-center gap-1.5 text-[13px] font-medium"><Icon className="size-3.5 text-muted-foreground" />{roleLabel[role]}</div>
                <ul className="flex flex-col gap-0.5">
                  {can.map((c) => (
                    <li key={c} className="flex items-start gap-1.5 text-[12.5px] leading-snug text-muted-foreground">
                      {c.startsWith("Cannot") ? <X className="mt-0.5 size-3 shrink-0 text-status-bad" /> : <Check className="mt-0.5 size-3 shrink-0 text-status-ok" />}{c}
                    </li>
                  ))}
                </ul>
              </div>
            ))}
          </div>
        </section>

        <ErrorNote error={update.error} className="mb-3" />
        <ErrorNote error={members.error} className="mb-3" />
        {members.isPending && <div className="flex justify-center py-8"><Spinner className="size-5 text-muted-foreground" /></div>}

        {members.data && (
          <div className="overflow-x-auto rounded-lg border bg-card">
            <table className="w-full">
              <thead className="border-b bg-muted/30"><tr>
                <th className={th}>Person</th><th className={th}>Role</th><th className={th}>Status</th><th className={th}>Joined</th>
                {manager && <th className={cn(th, "w-36")}><span className="sr-only">Actions</span></th>}
              </tr></thead>
              <tbody>
                {rows.map((m) => {
                  const self = m.id === viewer.userId
                  const options = assignableRoles(viewer.role, m.role, self)
                  const label = (m.name || m.email) + (self ? " (you)" : "")
                  return (
                    <tr key={m.id} className={cn(tr, !m.active && "opacity-60")}>
                      <td className={td}><Person size="md" name={label} initials={initials(m.name || m.email)} sub={m.email} /></td>
                      <td className={td}>
                        {options.length === 0 || !m.active ? (
                          <span className="flex h-7 items-center gap-1.5 px-2.5 text-[13px]">{m.role === "owner" && <Crown className="size-3.5 text-muted-foreground" />}{roleLabel[m.role]}</span>
                        ) : (
                          <Select value={m.role} onValueChange={(v) => setRole(m, v as Role)} disabled={update.isPending}>
                            <SelectTrigger size="sm" className="w-32 text-[13px]" aria-label={`Role of ${label}`}><SelectValue /></SelectTrigger>
                            <SelectContent>
                              {[m.role, ...options.filter((o) => o !== m.role)].map((x) =>
                                <SelectItem key={x} value={x}>{x === "owner" ? "Owner (transfer)" : roleLabel[x]}</SelectItem>)}
                            </SelectContent>
                          </Select>
                        )}
                      </td>
                      <td className={td}>{m.active ? <StatusChip status="ok">active</StatusChip> : <StatusChip status="idle">deactivated</StatusChip>}</td>
                      <td className={cn(td, "whitespace-nowrap text-muted-foreground")}>{fmtWhen(m.created_at)}</td>
                      {manager && (
                        <td className={cn(td, "py-0 text-right")}>
                          {canDeactivate(viewer.role, m.role, self) && (m.active
                            ? <Button size="xs" variant="ghost" className="text-status-bad hover:text-status-bad" onClick={() => setConfirm({ kind: "deactivate", member: m })}>Deactivate</Button>
                            : <Button size="xs" variant="outline" onClick={() => setConfirm({ kind: "reactivate", member: m })}>Reactivate</Button>)}
                        </td>
                      )}
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
        <p className="mt-3 text-[12px] text-muted-foreground">
          {manager ? "Role changes take effect on the person's next request and are written to the audit log." : "Only owners and admins change roles or deactivate people."}
        </p>
      </PageBody>
      <ConfirmDialog open={!!confirm} destructive={confirm?.kind === "deactivate"}
        title={confirm?.kind === "transfer" ? "Transfer ownership?" : confirm?.kind === "deactivate" ? "Deactivate this person?" : "Reactivate this person?"}
        body={confirm?.kind === "transfer" ? `${confirm.member.name || confirm.member.email} becomes the Owner. There is one Owner, so ownership moves to them.`
          : confirm?.kind === "deactivate" ? `${confirm.member.name || confirm.member.email} is signed out everywhere and can no longer sign in. Their sessions stay.`
            : "They can sign in again with their existing password."}
        confirm={confirm?.kind === "transfer" ? "Transfer" : confirm?.kind === "deactivate" ? "Deactivate" : "Reactivate"}
        onConfirm={doConfirm} onCancel={() => setConfirm(null)} />
    </SettingsFrame>
  )
}
