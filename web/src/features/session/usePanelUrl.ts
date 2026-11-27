import { useState } from "react"
import { useSearchParams } from "react-router"
import type { PanelState, SessionState } from "./types"
import { findView } from "./selectors"

/**
 * The view panel, mirrored into the URL: `?view=<view id>` while the panel shows a view, no
 * param while it is closed. A reload or a shared link reopens that view pinned. Tabs and
 * peek or fullscreen are local to this tab.
 */
export function usePanelUrl(state: SessionState): [PanelState, (p: PanelState) => void] {
  const [params, setParams] = useSearchParams()
  const viewId = params.get("view")
  const [local, setLocal] = useState<PanelState>({ mode: "closed", tabs: [] })

  let panel: PanelState = { ...local, mode: "closed" }
  if (viewId) {
    const known = local.tabs.find((t) => t.id === viewId) ?? findView(state, viewId)
    if (known) {
      const tabs = local.tabs.some((t) => t.id === viewId) ? local.tabs : [...local.tabs, known]
      panel = { ...local, tabs, active: viewId, mode: local.mode === "closed" ? "pinned" : local.mode }
    }
  }

  const set = (p: PanelState) => {
    setLocal(p)
    const next = p.mode === "closed" ? null : p.active ?? p.tabs[0]?.id ?? null
    if (next === viewId) return
    setParams((prev) => {
      const q = new URLSearchParams(prev)
      if (next) q.set("view", next); else q.delete("view")
      return q
    }, { replace: true })
  }
  return [panel, set]
}
