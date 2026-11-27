import { useState } from "react"
import { Link, useNavigate, useParams } from "react-router"
import { CalendarX2, Lock } from "lucide-react"
import { useAcceptInvite, useInvitePreview, type InvitePreview, type InviteState, type Role } from "@/api"
import { Avatar, AvatarFallback } from "@/components/ui/avatar"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardFooter, CardHeader } from "@/components/ui/card"
import { Field, FieldError, FieldGroup, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { InputGroup, InputGroupAddon, InputGroupInput } from "@/components/ui/input-group"
import { Kbd } from "@/components/ui/kbd"
import { Spinner } from "@/components/ui/spinner"
import { ErrorNote } from "@/app/shell/ErrorNote"
import { initials } from "@/features/auth/principal"
import { hasErrors, newPasswordError, requiredError, type Errors } from "@/features/auth/validate"
import { roleLabel } from "@/app/pages/common"
import { AuthShell, PasswordField } from "./AuthLayout"

const ROLE_HINT: Record<Role, string> = {
  owner: "Full control of the organisation, its members and settings.",
  admin: "Manages members and settings, and can read sessions (audited).",
  member: "Starts sessions with agents and sees their own work.",
  viewer: "Reads sessions shared with them. Cannot start runs.",
}

const CLOSED: Record<Exclude<InviteState, "valid">, { title: string; body: string }> = {
  expired: { title: "This invite has expired", body: "Ask an admin for a new one. Invite links work for 7 days and only once." },
  revoked: { title: "This invite was revoked", body: "An admin withdrew it. Ask them for a new one if you still need access." },
  used: { title: "This invite was already used", body: "Each link works once. If it was you, sign in instead." },
}

function daysLeft(iso?: string | null): string | undefined {
  if (!iso) return undefined
  const ms = new Date(iso).getTime() - Date.now()
  if (!Number.isFinite(ms) || ms <= 0) return undefined
  const d = Math.floor(ms / 86_400_000)
  return d >= 1 ? `${d} day${d === 1 ? "" : "s"}` : "less than a day"
}

/** /invite/:token. Invited by X as <Role> to <Org>; the email is fixed by the invite. */
export function AcceptInvite() {
  const { token = "" } = useParams()
  const preview = useInvitePreview(token)
  if (preview.isPending) return <AuthShell><div className="flex justify-center py-10"><Spinner className="size-5 text-muted-foreground" /></div></AuthShell>
  if (preview.error || !preview.data) {
    return (
      <AuthShell>
        <Card className="gap-4 py-6 shadow-sm">
          <CardContent className="flex flex-col gap-3 px-6">
            <p className="font-medium">This invite link does not work</p>
            <ErrorNote error={preview.error} />
            <Button variant="outline" asChild><Link to="/sign-in">Sign in instead</Link></Button>
          </CardContent>
        </Card>
      </AuthShell>
    )
  }
  return <AcceptInviteView token={token} invite={preview.data} />
}

export function AcceptInviteView({ token, invite }: { token: string; invite: InvitePreview }) {
  const navigate = useNavigate()
  const accept = useAcceptInvite(token)
  const [name, setName] = useState("")
  const [pw, setPw] = useState("")
  const [errors, setErrors] = useState<Errors<"name" | "password">>({})
  const inviter = invite.inviter || "An admin"

  const head = (
    <CardHeader className="gap-3 px-6">
      <div className="flex items-center gap-3">
        <Avatar className="size-9"><AvatarFallback className="text-xs">{initials(inviter)}</AvatarFallback></Avatar>
        <div className="min-w-0 leading-snug">
          <p><span className="font-medium">{inviter}</span> <span className="text-muted-foreground">invited you to</span> <span className="font-medium">{invite.org}</span></p>
          <p className="flex items-center gap-1.5 text-xs text-muted-foreground">as <Badge variant="secondary" className="h-5 px-1.5 text-[11px]">{roleLabel[invite.role]}</Badge></p>
        </div>
      </div>
    </CardHeader>
  )

  if (invite.state !== "valid") {
    const c = CLOSED[invite.state]
    return (
      <AuthShell>
        <Card className="gap-5 py-6 shadow-sm">
          {head}
          <CardContent className="px-6">
            <div role="alert" className="flex items-start gap-3 rounded-lg border border-status-bad/30 bg-status-bad-soft px-3.5 py-3">
              <CalendarX2 className="mt-0.5 size-4 shrink-0 text-status-bad" />
              <div className="flex flex-col gap-0.5">
                <p className="font-medium">{c.title}</p>
                <p className="text-[13px] text-muted-foreground">{c.body}</p>
              </div>
            </div>
            <dl className="mt-4 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 text-[13px]">
              <dt className="text-muted-foreground">For</dt><dd className="font-mono text-xs leading-5">{invite.email}</dd>
              <dt className="text-muted-foreground">Sent by</dt><dd>{inviter}</dd>
            </dl>
          </CardContent>
          <CardFooter className="flex gap-2 border-t bg-muted/40 px-6 py-3.5">
            <Button variant="outline" className="flex-1" asChild><Link to="/sign-in">Sign in instead</Link></Button>
          </CardFooter>
        </Card>
      </AuthShell>
    )
  }

  const submit = (e: React.FormEvent) => {
    e.preventDefault()
    const found = { name: requiredError(name, "your name"), password: newPasswordError(pw) }
    setErrors(found)
    if (hasErrors(found)) return
    accept.mutate({ name: name.trim(), password: pw }, { onSuccess: () => navigate("/", { replace: true }) })
  }
  const left = daysLeft(invite.expires_at)

  return (
    <AuthShell>
      <Card className="gap-5 py-6 shadow-sm">
        {head}
        <CardContent className="px-6">
          <p className="-mt-1 mb-4 rounded-md bg-muted/60 px-3 py-2 text-xs text-muted-foreground">
            <span className="font-medium text-foreground">{roleLabel[invite.role]}:</span> {ROLE_HINT[invite.role]}
          </p>
          <form onSubmit={submit} noValidate aria-label="Accept invite">
            <FieldGroup className="gap-4">
              <ErrorNote error={accept.error} />
              <Field className="gap-1.5">
                <FieldLabel htmlFor="ai-email">Email</FieldLabel>
                <InputGroup className="bg-muted/50">
                  <InputGroupInput id="ai-email" value={invite.email} readOnly aria-readonly className="text-muted-foreground" />
                  <InputGroupAddon align="inline-end"><Lock className="size-3.5" /></InputGroupAddon>
                </InputGroup>
                <span className="text-xs text-muted-foreground">Set by the invite</span>
              </Field>
              <Field className="gap-1.5" data-invalid={errors.name ? true : undefined}>
                <FieldLabel htmlFor="ai-name">Name</FieldLabel>
                <Input id="ai-name" value={name} onChange={(e) => setName(e.target.value)} autoComplete="name" aria-invalid={errors.name ? true : undefined} />
                {errors.name && <FieldError className="text-xs">{errors.name}</FieldError>}
              </Field>
              <PasswordField id="ai-pw" label="Password" value={pw} onChange={setPw} meter error={errors.password} />
              <Button type="submit" className="w-full" disabled={accept.isPending}>
                {accept.isPending && <Spinner className="size-3.5" />}Join {invite.org}
                <Kbd className="ml-1 bg-primary-foreground/15 text-primary-foreground">↵</Kbd>
              </Button>
            </FieldGroup>
          </form>
        </CardContent>
        <CardFooter className="border-t bg-muted/40 px-6 py-3.5 text-xs text-muted-foreground">
          Not you? Ignore this link.{left && ` It expires in ${left}.`}
        </CardFooter>
      </Card>
    </AuthShell>
  )
}
