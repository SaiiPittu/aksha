import { useState } from "react"
import { useNavigate } from "react-router"
import { Check, Crown, Lock, Mail } from "lucide-react"
import { useSignup } from "@/api"
import { cn } from "@/lib/utils"
import { Button } from "@/components/ui/button"
import { Card } from "@/components/ui/card"
import { Field, FieldError, FieldGroup, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { InputGroup, InputGroupAddon, InputGroupInput } from "@/components/ui/input-group"
import { Kbd } from "@/components/ui/kbd"
import { Spinner } from "@/components/ui/spinner"
import { ErrorNote } from "@/app/shell/ErrorNote"
import { emailError, hasErrors, newPasswordError, requiredError, type Errors } from "@/features/auth/validate"
import { AuthShell, PasswordField } from "./AuthLayout"

function StepHead({ n, title, done, active }: { n: number; title: string; done?: boolean; active?: boolean }) {
  return (
    <div className="flex items-center gap-2.5">
      <span className={cn("grid size-5 place-items-center rounded-full text-[11px] font-semibold",
        done ? "bg-foreground text-background" : active ? "border border-foreground text-foreground" : "border text-muted-foreground")}>
        {done ? <Check className="size-3" strokeWidth={3} /> : n}
      </span>
      <span className={cn("text-sm font-medium", !done && !active && "text-muted-foreground")}>{title}</span>
    </div>
  )
}

type K = "name" | "email" | "password" | "org"

/** First sign-up on a fresh server: 1 your account, 2 your organisation. You become the Owner. */
export function CreateOrg() {
  const navigate = useNavigate()
  const signup = useSignup()
  const [name, setName] = useState("")
  const [email, setEmail] = useState("")
  const [pw, setPw] = useState("")
  const [org, setOrg] = useState("")
  const [errors, setErrors] = useState<Errors<K>>({})
  const step1 = !!name.trim() && !emailError(email) && !newPasswordError(pw)

  const submit = (e: React.FormEvent) => {
    e.preventDefault()
    const found = { name: requiredError(name, "your name"), email: emailError(email), password: newPasswordError(pw), org: requiredError(org, "an organisation name") }
    setErrors(found)
    if (hasErrors(found)) return
    signup.mutate({ name: name.trim(), email: email.trim(), password: pw, org_name: org.trim() }, {
      onSuccess: () => navigate("/", { replace: true }),
    })
  }

  return (
    <AuthShell wide>
      <div className="mb-5">
        <h1 className="text-lg font-semibold tracking-tight">Set up this server</h1>
        <p className="text-muted-foreground">No one has signed up here yet. Create your account and your organisation.</p>
      </div>
      <div className="grid gap-5 md:grid-cols-[1fr_260px]">
        <Card className="gap-0 py-0 shadow-sm">
          <form onSubmit={submit} noValidate aria-label="Create organisation">
            {signup.error && <div className="px-6 pt-5"><ErrorNote error={signup.error} /></div>}
            <section className="px-6 pt-5 pb-6">
              <StepHead n={1} title="Your account" done={step1} />
              <FieldGroup className="mt-4 gap-4 pl-7.5">
                <div className="grid gap-3 sm:grid-cols-2">
                  <Field className="gap-1.5" data-invalid={errors.name ? true : undefined}>
                    <FieldLabel htmlFor="co-name">Name</FieldLabel>
                    <Input id="co-name" value={name} onChange={(e) => setName(e.target.value)} autoComplete="name" aria-invalid={errors.name ? true : undefined} />
                    {errors.name && <FieldError className="text-xs">{errors.name}</FieldError>}
                  </Field>
                  <Field className="gap-1.5" data-invalid={errors.email ? true : undefined}>
                    <FieldLabel htmlFor="co-email">Email</FieldLabel>
                    <InputGroup>
                      <InputGroupAddon><Mail /></InputGroupAddon>
                      <InputGroupInput id="co-email" type="email" autoComplete="email" value={email} onChange={(e) => setEmail(e.target.value)} aria-invalid={errors.email ? true : undefined} />
                    </InputGroup>
                    {errors.email && <FieldError className="text-xs">{errors.email}</FieldError>}
                  </Field>
                </div>
                <PasswordField id="co-pw" label="Password" value={pw} onChange={setPw} meter error={errors.password}
                  hint="At least 8 characters. Longer is better than clever." />
              </FieldGroup>
            </section>
            <section className="border-t px-6 pt-5 pb-6">
              <StepHead n={2} title="Your organisation" active={step1} />
              <FieldGroup className="mt-4 gap-4 pl-7.5">
                <Field className="gap-1.5" data-invalid={errors.org ? true : undefined}>
                  <FieldLabel htmlFor="co-org">Organisation name</FieldLabel>
                  <Input id="co-org" value={org} onChange={(e) => setOrg(e.target.value)} aria-invalid={errors.org ? true : undefined} />
                  {errors.org && <FieldError className="text-xs">{errors.org}</FieldError>}
                </Field>
              </FieldGroup>
            </section>
            <div className="flex items-center justify-end border-t bg-muted/40 px-6 py-3.5">
              <Button type="submit" disabled={signup.isPending}>
                {signup.isPending && <Spinner className="size-3.5" />}Create organisation
                <Kbd className="ml-1 bg-primary-foreground/15 text-primary-foreground">↵</Kbd>
              </Button>
            </div>
          </form>
        </Card>
        <aside className="flex flex-col gap-3 self-start rounded-xl border bg-background p-4">
          <div className="flex items-center gap-2 font-medium"><Crown className="size-4 text-muted-foreground" />You become the Owner</div>
          <p className="text-[13px] leading-relaxed text-muted-foreground">Sign-up closes after this; everyone else joins by invite.</p>
          <ul className="flex flex-col gap-2 border-t pt-3 text-[13px] text-muted-foreground">
            <li className="flex gap-2"><Check className="mt-0.5 size-3.5 shrink-0 text-status-ok" />Invite people as Admin, Member or Viewer</li>
            <li className="flex gap-2"><Check className="mt-0.5 size-3.5 shrink-0 text-status-ok" />Create API tokens for the terminal</li>
            <li className="flex gap-2"><Check className="mt-0.5 size-3.5 shrink-0 text-status-ok" />Read the audit log</li>
          </ul>
          <div className="flex items-start gap-2 rounded-md bg-muted/60 px-2.5 py-2 text-xs text-muted-foreground">
            <Lock className="mt-px size-3.5 shrink-0" />Your data stays on this server.
          </div>
        </aside>
      </div>
    </AuthShell>
  )
}
