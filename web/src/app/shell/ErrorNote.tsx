import { isApiError } from "@/api"
import { cn } from "@/lib/utils"
import { Glyph } from "@/app/chat/bits"

/** A refusal or failure shown inline, with the server's code so it can be looked up. */
export function ErrorNote({ error, className }: { error: unknown; className?: string }) {
  if (!error) return null
  const code = isApiError(error) ? error.code : "error"
  const message = error instanceof Error ? error.message : String(error)
  return (
    <div role="alert" className={cn("flex items-start gap-2 rounded-md bg-status-bad-soft px-3 py-2 text-[13px] text-status-bad", className)}>
      <Glyph status="bad" className="mt-[5px]" />
      <span><span className="font-mono text-[12px] font-medium">{code}</span> · {message}</span>
    </div>
  )
}

/** A full-height centred message for loading and failed boots. */
export function Centered({ children }: { children: React.ReactNode }) {
  return <div className="grid min-h-svh place-items-center bg-background p-6 text-sm text-muted-foreground">{children}</div>
}
