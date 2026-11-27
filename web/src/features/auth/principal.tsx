import { createContext, useContext } from "react"
import type { Me, Role } from "@/api"

/** Who is using the app, as every screen sees it. */
export interface Viewer {
  userId: string | null
  name: string
  email: string | null
  initials: string
  role: Role
  org: string
  mode: "local" | "account" | "guest"
  guestExpiresAt?: string | null
}

export const initials = (n: string) =>
  n.split(/[\s@._-]+/).filter(Boolean).map((p) => p[0]).join("").slice(0, 2).toUpperCase() || "?"

export function viewerFrom(me: Me | null | undefined, mode: Viewer["mode"]): Viewer {
  const name = me?.user?.name || me?.user?.email || (mode === "local" ? "Local user" : mode === "guest" ? "Guest" : "You")
  return {
    userId: me?.user?.id ?? null,
    name,
    email: me?.user?.email ?? null,
    initials: initials(name),
    role: me?.role ?? "owner",
    org: me?.org?.name ?? (mode === "local" ? "This machine" : mode === "guest" ? "Guest sandbox" : ""),
    mode,
    guestExpiresAt: me?.guest_expires_at,
  }
}

const ViewerContext = createContext<Viewer | null>(null)
export const ViewerProvider = ViewerContext.Provider

export function useViewer(): Viewer {
  const v = useContext(ViewerContext)
  if (!v) throw new Error("useViewer is used outside the signed-in app")
  return v
}

// ---- role rules (the server enforces them; the UI hides what would be refused) -------

export const canManageOrg = (r: Role) => r === "owner" || r === "admin"
export const canCommand = (r: Role) => r !== "viewer"

/** Roles `actor` may assign to someone whose current role is `target`. Empty: not editable. */
export function assignableRoles(actor: Role, target: Role, self: boolean): Role[] {
  if (self || !canManageOrg(actor)) return []
  if (target === "owner") return []
  if (actor === "owner") return ["owner", "admin", "member", "viewer"]
  return ["admin", "member", "viewer"]
}

/** Whether `actor` may deactivate (or reactivate) a member whose role is `target`. */
export const canDeactivate = (actor: Role, target: Role, self: boolean) =>
  !self && canManageOrg(actor) && target !== "owner"
