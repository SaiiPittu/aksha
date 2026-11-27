import { useState } from "react"
import { Link, Navigate, Outlet, useLocation, useMatch } from "react-router"
import { useAuthStatus, useMe } from "@/api"
import { Button } from "@/components/ui/button"
import { Spinner } from "@/components/ui/spinner"
import { AppShell, type NavKey } from "@/app/shell/AppShell"
import { Banner } from "@/app/shell/Banner"
import { Centered, ErrorNote } from "@/app/shell/ErrorNote"
import { decideApp, decidePublic, type GuardDecision } from "@/features/auth/guard"
import { ViewerProvider, viewerFrom, type Viewer } from "@/features/auth/principal"
import { useSessionList } from "@/features/session"
import { ClaimDialog, SaveGuestDialog } from "./AccountDialogs"

function useGuardInput() {
  const status = useAuthStatus()
  const me = useMe()
  const loc = useLocation()
  return {
    status: status.data,
    me: me.data,
    error: (status.error ?? me.error) as Error | null,
    path: loc.pathname + loc.search,
    retry: () => { void status.refetch(); void me.refetch() },
  }
}

function Pending({ d, retry }: { d: Extract<GuardDecision, { kind: "loading" | "error" }>; retry: () => void }) {
  if (d.kind === "loading") return <Centered><Spinner className="size-5" /></Centered>
  return (
    <Centered>
      <div className="flex w-full max-w-sm flex-col gap-3">
        <p className="font-medium text-foreground">Aksha could not reach its server</p>
        <ErrorNote error={d.error} />
        <Button variant="outline" onClick={retry}>Try again</Button>
      </div>
    </Centered>
  )
}

const navFor = (path: string): NavKey =>
  path.startsWith("/inbox") ? "inbox" : path.startsWith("/sessions") ? "sessions" : path.startsWith("/files") ? "files"
    : path.startsWith("/evals") ? "evals" : path.startsWith("/settings") ? "settings" : "chat"

function ModeBanner({ viewer }: { viewer: Viewer }) {
  const [open, setOpen] = useState(false)
  if (viewer.mode === "local") {
    return <>
      <Banner tone="local" text="Local mode: no accounts yet, and you are the only user on this machine. Claim this workspace to invite people."
        action={{ label: "Claim this workspace", onClick: () => setOpen(true) }} />
      <ClaimDialog open={open} onOpenChange={setOpen} />
    </>
  }
  if (viewer.mode === "guest") {
    const until = viewer.guestExpiresAt ? new Date(viewer.guestExpiresAt) : null
    const when = until && !Number.isNaN(until.getTime()) ? ` It expires ${until.toLocaleString([], { dateStyle: "medium", timeStyle: "short" })}.` : ""
    return <>
      <Banner tone="guest" text={`Guest sandbox.${when} Save it as an account to keep your sessions.`}
        action={{ label: "Save as account", onClick: () => setOpen(true) }} />
      <SaveGuestDialog open={open} onOpenChange={setOpen} />
    </>
  }
  return null
}

/** The layout route for every signed-in screen: guard, viewer, shell, mode banner. */
export function AppGate() {
  const g = useGuardInput()
  const session = useMatch("/s/:session")?.params.session
  const { sessions } = useSessionList()
  const d = decideApp(g)
  if (d.kind === "redirect") return <Navigate to={d.to} replace />
  if (d.kind !== "allow") return <Pending d={d} retry={g.retry} />
  const viewer = viewerFrom(g.me, d.mode)
  return (
    <ViewerProvider value={viewer}>
      <AppShell viewer={viewer} sessions={sessions} activeSession={session} active={navFor(g.path)} banner={<ModeBanner viewer={viewer} />}>
        <Outlet />
      </AppShell>
    </ViewerProvider>
  )
}

/** The layout route for sign in, sign up and try: a signed-in visitor goes to the app. */
export function PublicGate({ page }: { page: "sign-in" | "sign-up" | "try" }) {
  const g = useGuardInput()
  const d = decidePublic(g, page)
  if (d.kind === "redirect") return <Navigate to={d.to} replace />
  if (d.kind !== "allow") return <Pending d={d} retry={g.retry} />
  return <Outlet />
}

export function NotFound() {
  return (
    <Centered>
      <div className="flex flex-col items-center gap-3">
        <p className="font-medium text-foreground">Nothing lives at this address</p>
        <Button variant="outline" asChild><Link to="/">Go to the app</Link></Button>
      </div>
    </Centered>
  )
}
