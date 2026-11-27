import { useState } from "react"
import { Link, useNavigate, useSearchParams } from "react-router"
import { ArrowRight, Link2 } from "lucide-react"
import { useAuthStatus, useLogin } from "@/api"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card"
import { Checkbox } from "@/components/ui/checkbox"
import { Field, FieldError, FieldGroup, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Kbd } from "@/components/ui/kbd"
import { Spinner } from "@/components/ui/spinner"
import { ErrorNote } from "@/app/shell/ErrorNote"
import { safeNext } from "@/features/auth/guard"
import { emailError, hasErrors, requiredError, type Errors } from "@/features/auth/validate"
import { AuthShell, PasswordField } from "./AuthLayout"

/** Sign in with email and password. The guest link shows only when the server allows guests. */
export function SignIn() {
  const [params] = useSearchParams()
  const navigate = useNavigate()
  const status = useAuthStatus()
  const login = useLogin()
  const [email, setEmail] = useState("")
  const [pw, setPw] = useState("")
  const [keep, setKeep] = useState(true)
  const [errors, setErrors] = useState<Errors<"email" | "password">>({})

  const submit = (e: React.FormEvent) => {
    e.preventDefault()
    const found = { email: emailError(email), password: requiredError(pw, "your password") }
    setErrors(found)
    if (hasErrors(found)) return
    login.mutate({ email: email.trim(), password: pw, remember: keep }, {
      onSuccess: () => navigate(safeNext(params.get("next")), { replace: true }),
    })
  }

  return (
    <AuthShell>
      <Card className="gap-5 py-6 shadow-sm">
        <CardHeader className="gap-1 px-6">
          <CardTitle className="text-base">Sign in</CardTitle>
          <CardDescription>to your organisation on this server</CardDescription>
        </CardHeader>
        <CardContent className="px-6">
          <form onSubmit={submit} noValidate aria-label="Sign in">
            <FieldGroup className="gap-4">
              <ErrorNote error={login.error} />
              <Field className="gap-1.5" data-invalid={errors.email ? true : undefined}>
                <FieldLabel htmlFor="si-email">Email</FieldLabel>
                <Input id="si-email" type="email" autoComplete="email" value={email} aria-invalid={errors.email ? true : undefined}
                  onChange={(e) => setEmail(e.target.value)} />
                {errors.email && <FieldError className="text-xs">{errors.email}</FieldError>}
              </Field>
              <PasswordField id="si-pw" label="Password" value={pw} onChange={setPw} error={errors.password} />
              <Field orientation="horizontal" className="gap-2">
                <Checkbox id="si-keep" checked={keep} onCheckedChange={(v) => setKeep(v === true)} />
                <FieldLabel htmlFor="si-keep" className="font-normal">Keep me signed in</FieldLabel>
                <span className="ml-auto text-xs text-muted-foreground">{keep ? "14 days" : "this browser session"}</span>
              </Field>
              <Button type="submit" className="w-full" disabled={login.isPending}>
                {login.isPending && <Spinner className="size-3.5" />}Sign in
                <Kbd className="ml-1 bg-primary-foreground/15 text-primary-foreground">↵</Kbd>
              </Button>
            </FieldGroup>
          </form>
        </CardContent>
        <CardFooter className="flex items-start gap-2.5 border-t bg-muted/40 px-6 py-3.5 text-xs text-muted-foreground">
          <Link2 className="mt-px size-3.5 shrink-0" />
          <p><span className="font-medium text-foreground">Have an invite link?</span> Open it directly. It sets up your account and drops you into the right organisation.</p>
        </CardFooter>
      </Card>
      {status.data?.guests && (
        <div className="mt-4 flex items-center justify-center gap-1.5 text-sm text-muted-foreground">
          <span>New here?</span>
          <Link to="/try" className="inline-flex items-center gap-1 font-medium text-foreground underline-offset-4 hover:underline">
            Try without an account<ArrowRight className="size-3.5" /></Link>
        </div>
      )}
    </AuthShell>
  )
}
