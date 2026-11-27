import { Link, useNavigate } from "react-router"
import { Clock, FileStack, Play, ShieldCheck, UserPlus } from "lucide-react"
import { useGuest } from "@/api"
import { cn } from "@/lib/utils"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card"
import { Kbd } from "@/components/ui/kbd"
import { Spinner } from "@/components/ui/spinner"
import { ErrorNote } from "@/app/shell/ErrorNote"
import { AuthShell } from "./AuthLayout"

const GETS = [
  { icon: Play, title: "A private sandbox", body: "Chat, tools and the view panel work as usual, in a space only you can see." },
  { icon: FileStack, title: "Your own files", body: "Attach documents, PDFs and tables and open them in the view panel." },
  { icon: ShieldCheck, title: "Nothing leaves it", body: "Actions that leave the system still ask you first.", tone: "text-status-ok" },
  { icon: Clock, title: "Expires in 24 h", body: "Save it as an account before then to keep your sessions." },
]

/** /try: the guest sandbox landing, shown only when the server allows guests. */
export function TryAksha() {
  const navigate = useNavigate()
  const guest = useGuest()
  return (
    <AuthShell>
      <Card className="gap-5 py-6 shadow-sm">
        <CardHeader className="gap-1 px-6">
          <CardTitle className="text-base">Try Aksha without an account</CardTitle>
          <CardDescription>A private sandbox to see how chat, tools and the view panel work.</CardDescription>
        </CardHeader>
        <CardContent className="px-6">
          <ul className="flex flex-col gap-3.5">
            {GETS.map(({ icon: Icon, title, body, tone }) => (
              <li key={title} className="flex gap-3">
                <span className="grid size-7 shrink-0 place-items-center rounded-md border bg-muted/40">
                  <Icon className={cn("size-3.5", tone ?? "text-muted-foreground")} />
                </span>
                <div className="leading-snug">
                  <p className="font-medium">{title}</p>
                  <p className="text-[13px] text-muted-foreground">{body}</p>
                </div>
              </li>
            ))}
          </ul>
          <div className="mt-5 flex flex-col gap-2">
            <ErrorNote error={guest.error} />
            <Button className="w-full" disabled={guest.isPending}
              onClick={() => guest.mutate(undefined, { onSuccess: () => navigate("/", { replace: true }) })}>
              {guest.isPending && <Spinner className="size-3.5" />}Start a sandbox
              <Kbd className="ml-1 bg-primary-foreground/15 text-primary-foreground">↵</Kbd>
            </Button>
            <p className="flex items-center justify-center gap-1.5 text-xs text-muted-foreground">
              <UserPlus className="size-3.5" />Like it? Save as account later. Your sessions come with you.
            </p>
          </div>
        </CardContent>
        <CardFooter className="flex items-center justify-center gap-4 border-t bg-muted/40 px-6 py-3.5 text-xs text-muted-foreground">
          <span>Have an account? <Link to="/sign-in" className="font-medium text-foreground underline-offset-4 hover:underline">Sign in</Link></span>
          <span aria-hidden>·</span>
          <span>Got an invite? Open its link</span>
        </CardFooter>
      </Card>
    </AuthShell>
  )
}
