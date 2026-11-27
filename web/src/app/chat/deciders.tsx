// Who decided an interrupt, as the viewer reads it. The server records `by` as the principal
// (`user:<id>`, or `local` in local mode; ADR-P19 B19): the viewer's own is "you", another
// member's is their name from /org/members when the viewer may read it (an Admin or the
// Owner), else "another member". A role name (a correction's domain owner, an agent) stays.
import { createContext, useCallback, useContext, type ReactNode } from "react"
import { useMe, useMembers, type Me, type Member } from "@/api"

export type NameOf = (by: string | undefined) => string | undefined

export function deciderName(by: string | undefined, me: Me | null | undefined, members?: Member[]): string | undefined {
  if (!by) return undefined
  const own = me ? (me.user ? `user:${me.user.id}` : "local") : undefined
  if (by === own) return "you"
  if (by.startsWith("user:")) {
    const id = by.slice("user:".length)
    const m = members?.find((x) => x.id === id || x.user_id === id)
    return m ? m.name || m.email : "another member"
  }
  if (by === "local") return "the local user"
  return by
}

const Names = createContext<NameOf>((by) => by)

export const useNameOf = () => useContext(Names)

/** Provides the viewer's naming of deciders to the cards below it. */
export function DeciderNames({ children }: { children: ReactNode }) {
  const me = useMe().data
  const mayList = !!me?.user && (me.role === "owner" || me.role === "admin")
  const members = useMembers(mayList).data
  const nameOf = useCallback<NameOf>((by) => deciderName(by, me, mayList ? members : undefined), [me, members, mayList])
  return <Names.Provider value={nameOf}>{children}</Names.Provider>
}
