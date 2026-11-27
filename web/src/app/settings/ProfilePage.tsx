import { useState } from "react"
import { useChangePassword } from "@/api"
import { Button } from "@/components/ui/button"
import { FieldGroup } from "@/components/ui/field"
import { Spinner } from "@/components/ui/spinner"
import { ErrorNote } from "@/app/shell/ErrorNote"
import { PasswordField } from "@/app/auth/AuthLayout"
import { PageBody, PageHeader, Person, roleLabel } from "@/app/pages/common"
import { useViewer } from "@/features/auth/principal"
import { hasErrors, newPasswordError, requiredError, type Errors } from "@/features/auth/validate"
import { SettingsFrame } from "./SettingsPage"

export function ProfilePage() {
  const viewer = useViewer()
  return (
    <SettingsFrame active="profile">
      <PageHeader title="Profile" sub="Your account on this server" />
      <PageBody className="flex max-w-[720px] flex-col gap-5">
        <section className="rounded-lg border bg-card p-4">
          <Person size="md" name={viewer.name} initials={viewer.initials} sub={viewer.email ?? undefined} />
          <dl className="mt-4 grid grid-cols-[140px_1fr] gap-y-1.5 text-[13px]">
            <dt className="text-muted-foreground">Organisation</dt><dd>{viewer.org}</dd>
            <dt className="text-muted-foreground">Role</dt><dd>{roleLabel[viewer.role]}</dd>
            <dt className="text-muted-foreground">Signed in as</dt>
            <dd>{viewer.mode === "local" ? "Local mode (no account)" : viewer.mode === "guest" ? "Guest" : "Account"}</dd>
          </dl>
        </section>
        {viewer.mode === "account" && <ChangePassword />}
      </PageBody>
    </SettingsFrame>
  )
}

function ChangePassword() {
  const change = useChangePassword()
  const [cur, setCur] = useState("")
  const [next, setNext] = useState("")
  const [again, setAgain] = useState("")
  const [errors, setErrors] = useState<Errors<"cur" | "next" | "again">>({})
  const submit = (e: React.FormEvent) => {
    e.preventDefault()
    const found = {
      cur: requiredError(cur, "your current password"),
      next: newPasswordError(next),
      again: again === next ? undefined : "The passwords do not match.",
    }
    setErrors(found)
    if (hasErrors(found)) return
    change.mutate({ current: cur, new: next }, { onSuccess: () => { setCur(""); setNext(""); setAgain("") } })
  }
  return (
    <section className="rounded-lg border bg-card p-4">
      <h2 className="text-[13.5px] font-semibold">Change password</h2>
      <p className="mb-3 text-[12.5px] text-muted-foreground">This signs you out everywhere, including here.</p>
      <form onSubmit={submit} noValidate aria-label="Change password">
        <FieldGroup className="gap-4">
          <ErrorNote error={change.error} />
          {change.isSuccess && <p role="status" className="text-[13px] text-status-ok">✓ Password changed.</p>}
          <PasswordField id="pw-cur" label="Current password" value={cur} onChange={setCur} error={errors.cur} />
          <PasswordField id="pw-new" label="New password" value={next} onChange={setNext} meter error={errors.next} />
          <PasswordField id="pw-again" label="New password again" value={again} onChange={setAgain} error={errors.again} autoComplete="new-password" />
          <div><Button type="submit" disabled={change.isPending}>{change.isPending && <Spinner className="size-3.5" />}Change password</Button></div>
        </FieldGroup>
      </form>
    </section>
  )
}
