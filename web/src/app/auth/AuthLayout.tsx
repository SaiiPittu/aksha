import { useState } from "react"
import { Eye, EyeOff } from "lucide-react"
import { cn } from "@/lib/utils"
import { Field, FieldDescription, FieldError, FieldLabel } from "@/components/ui/field"
import { InputGroup, InputGroupAddon, InputGroupButton, InputGroupInput } from "@/components/ui/input-group"

/** The host this app is served from (the server it talks to). */
export const serverHost = () => (typeof window === "undefined" ? "" : window.location.host)

/** The small product mark: a square glyph and the name. */
export function ProductMark({ className }: { className?: string }) {
  return (
    <div className={cn("flex items-center gap-2", className)}>
      <span className="grid size-7 place-items-center rounded-md bg-foreground text-[13px] font-bold text-background">A</span>
      <span className="text-[15px] font-semibold tracking-tight">Aksha</span>
    </div>
  )
}

/** Centered, calm page frame shared by every signed-out screen. */
export function AuthShell({ children, footer, wide }: { children: React.ReactNode; footer?: React.ReactNode; wide?: boolean }) {
  return (
    <div className="flex min-h-svh flex-col bg-muted/40 text-sm text-foreground">
      <header className="flex h-14 items-center px-6">
        <ProductMark />
      </header>
      <main className="flex flex-1 items-center justify-center px-4 pb-6">
        <div className={cn("w-full", wide ? "max-w-[820px]" : "max-w-[400px]")}>{children}</div>
      </main>
      <footer className="flex h-12 items-center justify-center gap-2 text-xs text-muted-foreground">
        {footer ?? <ServerFooter />}
      </footer>
    </div>
  )
}

export function ServerFooter({ host = serverHost() }: { host?: string }) {
  return (
    <>
      <span className="size-1.5 rounded-full bg-status-ok" aria-hidden />
      <span>Self-hosted</span><span aria-hidden>·</span><span className="font-mono">{host}</span>
    </>
  )
}

/** Password strength: 0-4 from length and character mix. A display aid, not a policy. */
export function scorePassword(pw: string): number {
  if (!pw) return 0
  let s = 0
  if (pw.length >= 8) s++
  if (pw.length >= 12) s++
  if (/[a-z]/.test(pw) && /[A-Z]/.test(pw)) s++
  if (/\d/.test(pw) && /[^A-Za-z0-9]/.test(pw)) s++
  return Math.max(1, s)
}
const STRENGTH = ["", "Weak", "Fair", "Good", "Strong"]

export function StrengthMeter({ value }: { value: string }) {
  const score = scorePassword(value)
  const weak = score === 1
  return (
    <div className="flex items-center gap-3" aria-live="polite">
      <div className="grid flex-1 grid-cols-4 gap-1">
        {[1, 2, 3, 4].map((i) => (
          <span key={i} className={cn("h-1 rounded-full", i <= score ? (weak ? "bg-status-bad" : "bg-foreground/70") : "bg-border")} />
        ))}
      </div>
      <span className={cn("w-12 text-right text-xs", weak ? "text-status-bad" : "text-muted-foreground")}>
        {score ? STRENGTH[score] : "—"}
      </span>
    </div>
  )
}

/** Password input with a show/hide toggle and an optional strength meter under it. */
export function PasswordField({ id, label, value, onChange, meter, hint, aside, error, autoComplete }: {
  id: string; label: string; value: string; onChange: (v: string) => void
  meter?: boolean; hint?: string; aside?: React.ReactNode; error?: string; autoComplete?: string
}) {
  const [show, setShow] = useState(false)
  return (
    <Field className="gap-1.5" data-invalid={error ? true : undefined}>
      <div className="flex items-center justify-between">
        <FieldLabel htmlFor={id}>{label}</FieldLabel>{aside}
      </div>
      <InputGroup>
        <InputGroupInput id={id} type={show ? "text" : "password"} value={value} onChange={(e) => onChange(e.target.value)}
          autoComplete={autoComplete ?? (meter ? "new-password" : "current-password")}
          aria-invalid={error ? true : undefined} />
        <InputGroupAddon align="inline-end">
          <InputGroupButton size="icon-xs" aria-label={show ? "Hide password" : "Show password"} onClick={() => setShow((v) => !v)}>
            {show ? <EyeOff /> : <Eye />}
          </InputGroupButton>
        </InputGroupAddon>
      </InputGroup>
      {meter && <StrengthMeter value={value} />}
      {hint && <FieldDescription className="text-xs">{hint}</FieldDescription>}
      {error && <FieldError className="text-xs">{error}</FieldError>}
    </Field>
  )
}
