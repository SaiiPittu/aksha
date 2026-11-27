// React Query keys and hooks over the endpoints. Screens use these, never `fetch`.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { ApiError } from "./client"
import { auth, org, reads, runtime, tokens } from "./endpoints"
import type { Role } from "./schemas"

export const keys = {
  status: ["auth", "status"] as const,
  me: ["auth", "me"] as const,
  invite: (token: string) => ["auth", "invite", token] as const,
  members: ["org", "members"] as const,
  invites: ["org", "invites"] as const,
  audit: (q: object) => ["org", "audit", q] as const,
  tokens: ["tokens"] as const,
  capabilities: ["runtime", "capabilities"] as const,
  sessions: (archived: boolean) => ["sessions", { archived }] as const,
  sessionsAll: ["sessions"] as const,
  session: (s: string) => ["session", s] as const,
  artifacts: (q: object) => ["artifacts", q] as const,
  artifact: (ref: string) => ["artifact", ref] as const,
  artifactDiff: (ref: string, against: string) => ["artifact", ref, "diff", against] as const,
  lineage: (ref: string, dir: string) => ["artifact", ref, "lineage", dir] as const,
  documents: ["documents"] as const,
  document: (docKey: string, version?: number) => ["document", docKey, version ?? null] as const,
}

const noRetryOn4xx = (n: number, e: unknown) => !(e instanceof ApiError && e.status >= 400 && e.status < 500) && n < 2

export const useAuthStatus = () => useQuery({ queryKey: keys.status, queryFn: auth.status, staleTime: 60_000, retry: noRetryOn4xx })

/** The signed-in principal, or `null` when the server answers 401 (signed out). */
export const useMe = () => useQuery({
  queryKey: keys.me,
  queryFn: async () => {
    try { return await auth.me() } catch (e) {
      if (e instanceof ApiError && e.status === 401 && e.code !== "setup_required") return null
      throw e
    }
  },
  staleTime: 30_000,
  retry: noRetryOn4xx,
})

/** After anything that changes who is signed in: refetch status and me. */
function useRefreshAuth() {
  const qc = useQueryClient()
  return async () => {
    await Promise.all([qc.invalidateQueries({ queryKey: keys.status }), qc.invalidateQueries({ queryKey: keys.me })])
  }
}

export function useLogin() {
  const refresh = useRefreshAuth()
  return useMutation({ mutationFn: auth.login, onSuccess: refresh })
}
export function useSignup() {
  const refresh = useRefreshAuth()
  return useMutation({ mutationFn: auth.signup, onSuccess: refresh })
}
export function useClaim() {
  const refresh = useRefreshAuth()
  return useMutation({ mutationFn: auth.claim, onSuccess: refresh })
}
export function useAcceptInvite(token: string) {
  const refresh = useRefreshAuth()
  return useMutation({ mutationFn: (b: { name: string; password: string }) => auth.acceptInvite(token, b), onSuccess: refresh })
}
export function useGuest() {
  const refresh = useRefreshAuth()
  return useMutation({ mutationFn: auth.guest, onSuccess: refresh })
}
export function useSaveGuest() {
  const refresh = useRefreshAuth()
  return useMutation({ mutationFn: auth.saveGuest, onSuccess: refresh })
}
export function useLogout() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: auth.logout,
    onSettled: () => { qc.clear(); return qc.invalidateQueries({ queryKey: keys.me }) },
  })
}
export function useChangePassword() {
  const refresh = useRefreshAuth()
  return useMutation({ mutationFn: auth.changePassword, onSuccess: refresh })
}

export const useInvitePreview = (token: string) =>
  useQuery({ queryKey: keys.invite(token), queryFn: () => auth.invitePreview(token), retry: noRetryOn4xx })

// ---- org ---------------------------------------------------------------------------

export const useMembers = (enabled = true) => useQuery({ queryKey: keys.members, queryFn: org.members, enabled, retry: noRetryOn4xx })

export function useUpdateMember() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ user, ...body }: { user: string; role?: Role; active?: boolean }) => org.updateMember(user, body),
    onSettled: () => qc.invalidateQueries({ queryKey: keys.members }),
  })
}

export const useInvites = (enabled = true) => useQuery({ queryKey: keys.invites, queryFn: org.invites, enabled, retry: noRetryOn4xx })

export function useCreateInvites() {
  const qc = useQueryClient()
  return useMutation({ mutationFn: org.createInvites, onSettled: () => qc.invalidateQueries({ queryKey: keys.invites }) })
}
export function useResendInvite() {
  const qc = useQueryClient()
  return useMutation({ mutationFn: org.resendInvite, onSettled: () => qc.invalidateQueries({ queryKey: keys.invites }) })
}
export function useRevokeInvite() {
  const qc = useQueryClient()
  return useMutation({ mutationFn: org.revokeInvite, onSettled: () => qc.invalidateQueries({ queryKey: keys.invites }) })
}

export const useAudit = (q: { limit?: number; before?: string; action?: string; actor?: string }, enabled = true) =>
  useQuery({ queryKey: keys.audit(q), queryFn: () => org.audit(q), enabled, retry: noRetryOn4xx, placeholderData: (p) => p })

// ---- tokens --------------------------------------------------------------------------

export const useTokens = () => useQuery({ queryKey: keys.tokens, queryFn: tokens.list, retry: noRetryOn4xx })
export function useCreateToken() {
  const qc = useQueryClient()
  return useMutation({ mutationFn: tokens.create, onSettled: () => qc.invalidateQueries({ queryKey: keys.tokens }) })
}
export function useRevokeToken() {
  const qc = useQueryClient()
  return useMutation({ mutationFn: tokens.revoke, onSettled: () => qc.invalidateQueries({ queryKey: keys.tokens }) })
}

// ---- runtime -------------------------------------------------------------------------

export const useCapabilities = () => useQuery({ queryKey: keys.capabilities, queryFn: runtime.capabilities, retry: noRetryOn4xx })
export const useVerify = () => useMutation({ mutationFn: runtime.verify })

// ---- session reads ---------------------------------------------------------------------

export const useSessionsQuery = (archived = false, enabled = true) =>
  useQuery({ queryKey: keys.sessions(archived), queryFn: () => reads.sessions(archived), enabled, retry: noRetryOn4xx, refetchInterval: 15_000 })

export const useSessionStatus = (session: string, enabled = true) =>
  useQuery({ queryKey: keys.session(session), queryFn: () => reads.session(session), enabled, retry: noRetryOn4xx })

export function useArchiveSession() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ session, archive }: { session: string; archive: boolean }) => (archive ? reads.archive(session) : reads.restore(session)),
    onSettled: () => qc.invalidateQueries({ queryKey: keys.sessionsAll }),
  })
}

export const useArtifacts = (q: { session?: string; run?: string } = {}, enabled = true) =>
  useQuery({ queryKey: keys.artifacts(q), queryFn: () => reads.artifacts(q), enabled, retry: noRetryOn4xx })

export const useArtifact = (ref: string | undefined) =>
  useQuery({ queryKey: keys.artifact(ref ?? ""), queryFn: () => reads.artifact(ref!), enabled: !!ref, retry: noRetryOn4xx, staleTime: 60_000 })

export const useArtifactDiff = (ref: string | undefined, against: string | undefined) =>
  useQuery({ queryKey: keys.artifactDiff(ref ?? "", against ?? ""), queryFn: () => reads.artifactDiff(ref!, against!), enabled: !!ref && !!against, retry: noRetryOn4xx, staleTime: 60_000 })

export const useLineage = (ref: string | undefined, dir: "up" | "down", enabled = true) =>
  useQuery({ queryKey: keys.lineage(ref ?? "", dir), queryFn: () => reads.lineage(ref!, dir), enabled: enabled && !!ref, retry: noRetryOn4xx, staleTime: 30_000 })

export const useDocuments = (enabled = true) =>
  useQuery({ queryKey: keys.documents, queryFn: reads.documents, enabled, retry: noRetryOn4xx })

export const useDocument = (docKey: string | undefined, version?: number) =>
  useQuery({ queryKey: keys.document(docKey ?? "", version), queryFn: () => reads.document(docKey!, version), enabled: !!docKey, retry: noRetryOn4xx, staleTime: 300_000 })
