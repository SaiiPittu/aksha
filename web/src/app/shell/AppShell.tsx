import { Link, useNavigate } from "react-router"
import { Inbox, LayoutList, FolderOpen, FlaskConical, Settings, Plus, ChevronsUpDown, LogOut, UserCircle } from "lucide-react"
import {
  Sidebar, SidebarContent, SidebarFooter, SidebarGroup, SidebarGroupContent, SidebarGroupLabel, SidebarHeader,
  SidebarInset, SidebarMenu, SidebarMenuBadge, SidebarMenuButton, SidebarMenuItem, SidebarProvider,
} from "@/components/ui/sidebar"
import { Button } from "@/components/ui/button"
import { Kbd } from "@/components/ui/kbd"
import { Avatar, AvatarFallback } from "@/components/ui/avatar"
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel, DropdownMenuSeparator, DropdownMenuTrigger } from "@/components/ui/dropdown-menu"
import { useLogout } from "@/api"
import type { Viewer } from "@/features/auth/principal"
import type { SessionSummary } from "@/features/session/types"
import { StatusDot } from "./Status"

export type NavKey = "chat" | "inbox" | "sessions" | "files" | "evals" | "settings"

const roleLabel = { owner: "Owner", admin: "Admin", member: "Member", viewer: "Viewer" } as const

const nav: { key: NavKey; to: string; label: string; icon: React.ElementType }[] = [
  { key: "inbox", to: "/inbox", label: "Waiting on you", icon: Inbox },
  { key: "sessions", to: "/sessions", label: "Sessions", icon: LayoutList },
  { key: "files", to: "/files", label: "Files and artifacts", icon: FolderOpen },
  { key: "evals", to: "/evals", label: "Evals", icon: FlaskConical },
]

/** The shared app frame: left rail (sessions, inbox, nav, account) and an inset main area. */
export function AppShell({ viewer, sessions, activeSession, active, banner, children }: {
  viewer: Viewer; sessions: SessionSummary[]; activeSession?: string; active: NavKey
  banner?: React.ReactNode; children: React.ReactNode
}) {
  const navigate = useNavigate()
  const logout = useLogout()
  const waiting = sessions.filter((s) => s.status === "wait" || s.waiting).length
  const recent = sessions.filter((s) => !s.archived).slice(0, 30)
  const signOut = () => logout.mutate(undefined, { onSettled: () => navigate("/sign-in", { replace: true }) })
  return (
    <SidebarProvider style={{ "--sidebar-width": "15.5rem" } as React.CSSProperties} className="h-svh">
      <Sidebar variant="sidebar" collapsible="icon">
        <SidebarHeader className="gap-2 px-3 pt-3">
          <div className="flex items-center gap-2 rounded-md px-1 py-1 text-left">
            <span className="grid size-6 place-items-center rounded-md bg-foreground text-[12px] font-bold text-background">A</span>
            <span className="flex-1 leading-tight"><span className="block text-sm font-semibold">Aksha</span>
              <span className="block text-xs text-muted-foreground">{viewer.org}</span></span>
            <ChevronsUpDown className="size-3.5 text-muted-foreground" />
          </div>
          {viewer.role !== "viewer" && (
            <Button className="w-full justify-start gap-2" size="sm" asChild>
              <Link to="/"><Plus className="size-4" />New chat<Kbd className="ml-auto">N</Kbd></Link>
            </Button>
          )}
        </SidebarHeader>
        <SidebarContent>
          <SidebarGroup>
            <SidebarGroupContent>
              <SidebarMenu>
                {nav.map(({ key, to, label, icon: Icon }) => (
                  <SidebarMenuItem key={key}>
                    <SidebarMenuButton asChild isActive={active === key}><Link to={to}><Icon />{label}</Link></SidebarMenuButton>
                    {key === "inbox" && waiting > 0 && <SidebarMenuBadge className="text-status-wait">{waiting}</SidebarMenuBadge>}
                  </SidebarMenuItem>
                ))}
              </SidebarMenu>
            </SidebarGroupContent>
          </SidebarGroup>
          <SidebarGroup>
            <SidebarGroupLabel>Recent</SidebarGroupLabel>
            <SidebarGroupContent>
              <SidebarMenu>
                {recent.length === 0 && <p className="px-2 py-1 text-xs text-muted-foreground group-data-[collapsible=icon]:hidden">No sessions yet.</p>}
                {recent.map((s) => (
                  <SidebarMenuItem key={s.id}>
                    <SidebarMenuButton asChild isActive={s.id === activeSession} className="h-auto py-1.5">
                      <Link to={`/s/${encodeURIComponent(s.id)}`}>
                        <StatusDot status={s.status} />
                        <span className="flex min-w-0 flex-col">
                          <span className="truncate">{s.title}</span>
                          <span className="truncate text-xs text-muted-foreground">{s.agent} · {s.statusLabel} · {s.age}</span>
                        </span>
                      </Link>
                    </SidebarMenuButton>
                  </SidebarMenuItem>
                ))}
              </SidebarMenu>
            </SidebarGroupContent>
          </SidebarGroup>
        </SidebarContent>
        <SidebarFooter>
          <SidebarMenu><SidebarMenuItem>
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <SidebarMenuButton size="lg" isActive={active === "settings"} aria-label="Account menu">
                  <Avatar className="size-7"><AvatarFallback className="text-xs">{viewer.initials}</AvatarFallback></Avatar>
                  <span className="flex min-w-0 flex-col leading-tight"><span className="truncate text-sm font-medium">{viewer.name}</span>
                    <span className="text-xs text-muted-foreground">{roleLabel[viewer.role]}</span></span>
                  <Settings className="ml-auto size-4 text-muted-foreground" />
                </SidebarMenuButton>
              </DropdownMenuTrigger>
              <DropdownMenuContent side="top" align="start" className="w-56">
                <DropdownMenuLabel className="text-xs font-normal text-muted-foreground">{viewer.email ?? viewer.name}</DropdownMenuLabel>
                <DropdownMenuItem asChild><Link to="/settings/profile"><UserCircle />Profile</Link></DropdownMenuItem>
                <DropdownMenuItem asChild><Link to="/settings/members"><Settings />Settings</Link></DropdownMenuItem>
                {viewer.mode !== "local" && <>
                  <DropdownMenuSeparator />
                  <DropdownMenuItem onSelect={signOut}><LogOut />Sign out</DropdownMenuItem>
                </>}
              </DropdownMenuContent>
            </DropdownMenu>
          </SidebarMenuItem></SidebarMenu>
        </SidebarFooter>
      </Sidebar>
      <SidebarInset className="min-w-0 overflow-hidden">
        {banner}
        <div className="flex min-h-0 flex-1 flex-col">{children}</div>
      </SidebarInset>
    </SidebarProvider>
  )
}
