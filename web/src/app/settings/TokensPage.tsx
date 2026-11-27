import { useState } from "react"
import { KeyRound } from "lucide-react"
import { useCreateToken, useRevokeToken, useTokens, type ApiToken } from "@/api"
import { cn } from "@/lib/utils"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Spinner } from "@/components/ui/spinner"
import { ErrorNote } from "@/app/shell/ErrorNote"
import { ConfirmDialog } from "@/app/shell/ConfirmDialog"
import { CopyField } from "@/app/shell/CopyField"
import { PageBody, PageEmpty, PageHeader, fmtWhen, td, th, tr } from "@/app/pages/common"
import { SettingsFrame } from "./SettingsPage"

/** API tokens let the terminal client reach this server: `AKSHA_TOKEN=<token> aksha ...`. */
export function TokensPage() {
  const tokens = useTokens()
  const create = useCreateToken()
  const revoke = useRevokeToken()
  const [name, setName] = useState("")
  const [error, setError] = useState<string>()
  const [revoking, setRevoking] = useState<ApiToken | null>(null)

  const submit = (e: React.FormEvent) => {
    e.preventDefault()
    if (!name.trim()) { setError("Name the token, for example after the machine that uses it."); return }
    setError(undefined)
    create.mutate({ name: name.trim() }, { onSuccess: () => setName("") })
  }
  const rows = tokens.data ?? []

  return (
    <SettingsFrame active="tokens">
      <PageHeader title="API tokens" sub="For the terminal client against this server. A token acts as you, with your role." />
      <PageBody className="flex flex-col gap-5">
        <form onSubmit={submit} noValidate aria-label="Create token" className="flex flex-col gap-3 rounded-lg border bg-card p-4">
          <h2 className="text-[13.5px] font-semibold">Create a token</h2>
          <div className="flex items-center gap-2">
            <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="Token name, e.g. work laptop" aria-label="Token name" className="h-8 max-w-sm text-[13px]" />
            <Button size="sm" type="submit" disabled={create.isPending}>{create.isPending && <Spinner className="size-3.5" />}Create token</Button>
          </div>
          {error && <p role="alert" className="text-[12.5px] text-status-bad">{error}</p>}
          <ErrorNote error={create.error} />
          {create.data && (
            <div className="flex flex-col gap-2 rounded-md border border-status-wait/40 bg-status-wait-soft/60 p-3">
              <p className="text-[12.5px]"><span className="font-medium">Copy this token now.</span> It is shown once and cannot be read again.</p>
              <CopyField value={create.data.secret} label="new API token" />
              <p className="font-mono text-[12px] text-muted-foreground">export AKSHA_TOKEN=…</p>
            </div>
          )}
        </form>

        <section className="flex flex-col gap-3">
          <h2 className="text-[13.5px] font-semibold">Your tokens</h2>
          <ErrorNote error={tokens.error} />
          <ErrorNote error={revoke.error} />
          {tokens.isPending ? <div className="flex justify-center py-6"><Spinner className="size-5 text-muted-foreground" /></div>
            : rows.length === 0 ? <PageEmpty icon={KeyRound} title="No tokens yet" body="Create one to use the terminal client against this server." /> : (
              <div className="overflow-x-auto rounded-lg border bg-card">
                <table className="w-full">
                  <thead className="border-b bg-muted/30"><tr>
                    <th className={th}>Name</th><th className={th}>Created</th><th className={th}>Last used</th><th className={cn(th, "w-28")}><span className="sr-only">Actions</span></th>
                  </tr></thead>
                  <tbody>
                    {rows.map((t) => (
                      <tr key={t.id} className={tr}>
                        <td className={cn(td, "font-mono text-[12.5px]")}>{t.name}</td>
                        <td className={cn(td, "text-muted-foreground")}>{fmtWhen(t.created_at)}</td>
                        <td className={cn(td, "text-muted-foreground")}>{fmtWhen(t.last_used_at)}</td>
                        <td className={cn(td, "py-0 text-right")}>
                          <Button size="xs" variant="ghost" className="text-status-bad hover:text-status-bad" onClick={() => setRevoking(t)}>Revoke</Button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
        </section>
      </PageBody>
      <ConfirmDialog open={!!revoking} destructive title="Revoke this token?" confirm="Revoke"
        body={`Anything using "${revoking?.name ?? ""}" stops working at once.`}
        onConfirm={() => { if (revoking) revoke.mutate(revoking.id); setRevoking(null) }} onCancel={() => setRevoking(null)} />
    </SettingsFrame>
  )
}
