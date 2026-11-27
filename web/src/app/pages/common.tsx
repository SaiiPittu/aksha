import { Activity, Building2, KeyRound, ScrollText, UserCircle, Users, MailPlus } from "lucide-react"
import { Link } from "react-router"
import { cn } from "@/lib/utils"
import { Avatar, AvatarFallback } from "@/components/ui/avatar"
import type { Role } from "@/api"

/** The bar at the top of every non-chat page: title, a muted subline, and actions on the right. */
export function PageHeader({ title, sub, children }: { title: string; sub?: React.ReactNode; children?: React.ReactNode }) {
  return (
    <header className="flex h-14 shrink-0 items-center gap-3 border-b px-6">
      <div className="min-w-0 flex-1">
        <h1 className="text-[15px] leading-tight font-semibold">{title}</h1>
        {sub && <p className="truncate text-[12.5px] text-muted-foreground">{sub}</p>}
      </div>
      {children}
    </header>
  )
}

/** Scrollable page body with a comfortable max width. */
export function PageBody({ children, className }: { children: React.ReactNode; className?: string }) {
  return <div className="min-h-0 flex-1 overflow-auto"><div className={cn("mx-auto w-full max-w-[1080px] px-6 py-5", className)}>{children}</div></div>
}

/** Segmented filter tabs with counts (All 6, Running 1 ...). */
export function FilterTabs<K extends string>({ items, value, onChange }: {
  items: { key: K; label: string; count?: number; tone?: string }[]; value: K; onChange: (k: K) => void
}) {
  return (
    <div role="tablist" className="inline-flex h-8 items-center rounded-lg bg-muted p-[3px]">
      {items.map((it) => (
        <button key={it.key} role="tab" aria-selected={value === it.key} onClick={() => onChange(it.key)}
          className={cn("inline-flex h-full items-center gap-1.5 rounded-md px-2.5 text-[13px] font-medium text-muted-foreground transition-colors hover:text-foreground",
            value === it.key && "bg-background text-foreground shadow-sm dark:bg-input/40")}>
          {it.label}
          {it.count !== undefined && <span className={cn("tabular-nums text-[11.5px] text-muted-foreground", it.tone)}>{it.count}</span>}
        </button>
      ))}
    </div>
  )
}

export const roleLabel: Record<Role, string> = { owner: "Owner", admin: "Admin", member: "Member", viewer: "Viewer" }

export function Person({ name, initials, sub, size = "sm" }: { name: string; initials: string; sub?: string; size?: "sm" | "md" }) {
  return (
    <span className="flex min-w-0 items-center gap-2">
      <Avatar className={size === "md" ? "size-7" : "size-5"}><AvatarFallback className={size === "md" ? "text-[11px]" : "text-[9.5px]"}>{initials}</AvatarFallback></Avatar>
      <span className="flex min-w-0 flex-col leading-tight">
        <span className="truncate">{name}</span>
        {sub && <span className="truncate text-[12px] text-muted-foreground">{sub}</span>}
      </span>
    </span>
  )
}

/** Dense table primitives used by every page (13px, quiet header, row hover). */
export const th = "h-8 px-3 text-left align-middle text-[11.5px] font-medium tracking-wide text-muted-foreground uppercase whitespace-nowrap"
export const td = "h-11 px-3 align-middle text-[13px]"
export const tr = "group border-b last:border-b-0 transition-colors hover:bg-muted/50"

export type SettingsKey = "profile" | "members" | "invites" | "tokens" | "audit" | "runtime"
const settingsNav: { key: SettingsKey; label: string; icon: React.ElementType; managers?: boolean }[] = [
  { key: "profile", label: "Profile", icon: UserCircle },
  { key: "members", label: "Members", icon: Users },
  { key: "invites", label: "Invites", icon: MailPlus, managers: true },
  { key: "tokens", label: "API tokens", icon: KeyRound },
  { key: "audit", label: "Audit log", icon: ScrollText, managers: true },
  { key: "runtime", label: "Runtime", icon: Activity },
]

/** Settings layout: a sub-nav column next to the page content. */
export function SettingsLayout({ active, org, manager, children }: { active: SettingsKey; org: string; manager: boolean; children: React.ReactNode }) {
  return (
    <div className="flex min-h-0 flex-1">
      <nav aria-label="Settings" className="flex w-52 shrink-0 flex-col gap-0.5 border-r bg-sidebar/40 p-3">
        <div className="mb-2 flex items-center gap-2 px-2 pt-1 text-[12px] text-muted-foreground"><Building2 className="size-3.5" />{org} settings</div>
        {settingsNav.filter((n) => manager || !n.managers).map(({ key, label, icon: Icon }) => (
          <Link key={key} to={`/settings/${key}`} aria-current={key === active ? "page" : undefined}
            className={cn("flex h-8 items-center gap-2 rounded-md px-2 text-[13px] text-muted-foreground hover:bg-muted hover:text-foreground",
              key === active && "bg-muted font-medium text-foreground")}>
            <Icon className="size-4" />{label}
          </Link>
        ))}
      </nav>
      <div className="flex min-w-0 flex-1 flex-col">{children}</div>
    </div>
  )
}

/** Short relative or absolute time for tables ("3 min ago", "26 Nov 2026"). */
export function fmtWhen(iso?: string | null): string {
  if (!iso) return "never"
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  const s = (Date.now() - d.getTime()) / 1000
  if (s >= 0 && s < 60) return "just now"
  if (s >= 0 && s < 3600) return `${Math.floor(s / 60)} min ago`
  if (s >= 0 && s < 86400) return `${Math.floor(s / 3600)} h ago`
  return d.toLocaleDateString([], { day: "numeric", month: "short", year: "numeric" })
}

/** A plain empty state for a page whose data is not served yet. */
export function PageEmpty({ icon: Icon, title, body }: { icon: React.ElementType; title: string; body: string }) {
  return (
    <div className="flex flex-col items-center gap-2 rounded-lg border border-dashed px-6 py-14 text-center">
      <span className="grid size-9 place-items-center rounded-md bg-muted text-muted-foreground"><Icon className="size-4" /></span>
      <p className="text-[14px] font-medium">{title}</p>
      <p className="max-w-md text-[13px] text-muted-foreground">{body}</p>
    </div>
  )
}
