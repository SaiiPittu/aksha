import { useState } from "react"
import { useClaim, useSaveGuest } from "@/api"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Field, FieldError, FieldGroup, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Spinner } from "@/components/ui/spinner"
import { ErrorNote } from "@/app/shell/ErrorNote"
import { emailError, hasErrors, newPasswordError, requiredError, type Errors } from "@/features/auth/validate"
import { PasswordField } from "./AuthLayout"

type K = "name" | "email" | "password" | "org"

function TextField({ id, label, value, onChange, error, type = "text", autoComplete }: {
  id: string; label: string; value: string; onChange: (v: string) => void; error?: string; type?: string; autoComplete?: string
}) {
  return (
    <Field className="gap-1.5" data-invalid={error ? true : undefined}>
      <FieldLabel htmlFor={id}>{label}</FieldLabel>
      <Input id={id} type={type} value={value} autoComplete={autoComplete} aria-invalid={error ? true : undefined} onChange={(e) => onChange(e.target.value)} />
      {error && <FieldError className="text-xs">{error}</FieldError>}
    </Field>
  )
}

/** Local mode → SignedIn: set an email and password, name the organisation, become its Owner. Work done so far is kept. */
export function ClaimDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (o: boolean) => void }) {
  const claim = useClaim()
  const [v, setV] = useState({ name: "", email: "", password: "", org: "" })
  const [errors, setErrors] = useState<Errors<K>>({})
  const set = (k: keyof typeof v) => (x: string) => setV((p) => ({ ...p, [k]: x }))
  const submit = (e: React.FormEvent) => {
    e.preventDefault()
    const found = { name: requiredError(v.name, "your name"), email: emailError(v.email), password: newPasswordError(v.password), org: requiredError(v.org, "an organisation name") }
    setErrors(found)
    if (hasErrors(found)) return
    claim.mutate({ name: v.name.trim(), email: v.email.trim(), password: v.password, org_name: v.org.trim() }, { onSuccess: () => onOpenChange(false) })
  }
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Claim this workspace</DialogTitle>
          <DialogDescription>You become the Owner and can invite other people. Everything done in local mode stays.</DialogDescription>
        </DialogHeader>
        <form onSubmit={submit} noValidate aria-label="Claim workspace">
          <FieldGroup className="gap-4">
            <ErrorNote error={claim.error} />
            <TextField id="cl-name" label="Name" value={v.name} onChange={set("name")} error={errors.name} autoComplete="name" />
            <TextField id="cl-email" label="Email" type="email" value={v.email} onChange={set("email")} error={errors.email} autoComplete="email" />
            <PasswordField id="cl-pw" label="Password" value={v.password} onChange={set("password")} meter error={errors.password} />
            <TextField id="cl-org" label="Organisation name" value={v.org} onChange={set("org")} error={errors.org} />
          </FieldGroup>
          <DialogFooter className="mt-5">
            <Button type="button" variant="ghost" onClick={() => onOpenChange(false)}>Not now</Button>
            <Button type="submit" disabled={claim.isPending}>{claim.isPending && <Spinner className="size-3.5" />}Claim workspace</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

/** Guest → SignedIn: keep the sandbox's sessions under a real account. */
export function SaveGuestDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (o: boolean) => void }) {
  const save = useSaveGuest()
  const [v, setV] = useState({ name: "", email: "", password: "" })
  const [errors, setErrors] = useState<Errors<"name" | "email" | "password">>({})
  const set = (k: keyof typeof v) => (x: string) => setV((p) => ({ ...p, [k]: x }))
  const submit = (e: React.FormEvent) => {
    e.preventDefault()
    const found = { name: requiredError(v.name, "your name"), email: emailError(v.email), password: newPasswordError(v.password) }
    setErrors(found)
    if (hasErrors(found)) return
    save.mutate({ name: v.name.trim(), email: v.email.trim(), password: v.password }, { onSuccess: () => onOpenChange(false) })
  }
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Save as account</DialogTitle>
          <DialogDescription>Your sessions come with you and stay in your own workspace. Joining an organisation takes an invite from its admin.</DialogDescription>
        </DialogHeader>
        <form onSubmit={submit} noValidate aria-label="Save as account">
          <FieldGroup className="gap-4">
            <ErrorNote error={save.error} />
            <TextField id="sg-name" label="Name" value={v.name} onChange={set("name")} error={errors.name} autoComplete="name" />
            <TextField id="sg-email" label="Email" type="email" value={v.email} onChange={set("email")} error={errors.email} autoComplete="email" />
            <PasswordField id="sg-pw" label="Password" value={v.password} onChange={set("password")} meter error={errors.password} />
          </FieldGroup>
          <DialogFooter className="mt-5">
            <Button type="button" variant="ghost" onClick={() => onOpenChange(false)}>Not now</Button>
            <Button type="submit" disabled={save.isPending}>{save.isPending && <Spinner className="size-3.5" />}Save account</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
