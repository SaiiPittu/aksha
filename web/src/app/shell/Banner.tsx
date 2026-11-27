import { Eye, Info, Laptop, UserRound } from "lucide-react"
import { cn } from "@/lib/utils"
import { Button } from "@/components/ui/button"

export type BannerProps = {
  tone: "info" | "wait" | "audit" | "guest" | "local"
  text: string
  action?: { label: string; onClick: () => void }
}

const bannerStyle = {
  local: { cls: "bg-muted/60", icon: Laptop },
  guest: { cls: "bg-muted/60", icon: UserRound },
  audit: { cls: "bg-foreground text-background", icon: Eye },
  wait: { cls: "bg-status-wait-soft text-foreground", icon: Info },
  info: { cls: "bg-muted/60", icon: Info },
} as const

/** A one-line strip under a header: local mode, guest sandbox, audited read, notices. */
export function Banner({ tone, text, action }: BannerProps) {
  const b = bannerStyle[tone]
  const Icon = b.icon
  return (
    <div className={cn("flex min-h-9 shrink-0 items-center gap-2.5 border-b px-4 py-1.5 text-[13px]", b.cls)}>
      {tone === "wait" ? <span aria-hidden className="text-[10px] text-status-wait">◆</span> : <Icon className="size-3.5 shrink-0 opacity-70" />}
      <span className="min-w-0 flex-1 truncate">{text}</span>
      {action && <Button size="xs" variant={tone === "audit" ? "secondary" : "outline"} className="shrink-0" onClick={action.onClick}>{action.label}</Button>}
    </div>
  )
}

