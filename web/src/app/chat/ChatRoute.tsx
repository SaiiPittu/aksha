import { useMemo } from "react"
import { useParams } from "react-router"
import { useCapabilities } from "@/api"
import { canCommand, useViewer } from "@/features/auth/principal"
import { usePanelUrl, useSession } from "@/features/session"
import type { AgentOption } from "./Composer"
import { ChatScreenView } from "./ChatScreenView"

/** The agents of the capability record (`agents[].name`, `purpose`), for the composer's picker. */
function useAgents(): AgentOption[] {
  const caps = useCapabilities()
  return useMemo(() => {
    const raw = (caps.data as { agents?: unknown } | undefined)?.agents
    if (!Array.isArray(raw)) return []
    return raw.flatMap((a) => (a && typeof a === "object" && typeof (a as { name?: unknown }).name === "string"
      ? [{ name: (a as { name: string }).name, purpose: typeof (a as { purpose?: unknown }).purpose === "string" ? (a as { purpose: string }).purpose : undefined }]
      : []))
  }, [caps.data])
}

/** `/` (new chat) and `/s/:session`. The panel lives in `?view=`; the session comes from `useSession`. */
export function ChatRoute() {
  const { session: sessionId = null } = useParams()
  const viewer = useViewer()
  const session = useSession(sessionId)
  const [panel, setPanel] = usePanelUrl(session.state)
  const agents = useAgents()
  const readOnly = canCommand(viewer.role) ? undefined : "You have read-only access (Viewer), so you cannot send messages or answer."
  // A key per session resets per-session UI (composer draft, open cards) on navigation.
  return <ChatScreenView key={sessionId ?? "new"} session={session} panel={panel} onPanelChange={setPanel} readOnly={readOnly} agents={agents} />
}
