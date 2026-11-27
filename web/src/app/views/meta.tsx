import {
  Activity, AppWindow, Code2, FileCode, FileText, Frame, GitCompare, Globe, Image, LayoutDashboard,
  Link2, Package, Play, Puzzle, Quote, Sheet, Upload, Wrench, Braces,
} from "lucide-react"
import type { LucideIcon } from "lucide-react"
import type { ViewKind, ViewRef } from "@/features/session/types"

// One icon per view kind; plugin kinds share the puzzle piece.
const kindIcon: Record<string, LucideIcon> = {
  markdown: FileText, pdf: FileText, text: FileCode, json: Braces, table: Sheet, diff: GitCompare, image: Image,
  web: Globe, embed: Frame, html: Code2, "ui-spec": LayoutDashboard, trace: Activity,
}
export function KindIcon({ kind, className }: { kind: ViewKind; className?: string }) {
  const I = kindIcon[kind] ?? (kind.startsWith("plugin:") ? Puzzle : AppWindow)
  return <I className={className ?? "size-3.5"} aria-hidden />
}

export const kindLabel = (k: ViewKind) =>
  ({ markdown: "Markdown", pdf: "PDF", text: "Text", json: "JSON", table: "Table", diff: "Diff", image: "Image", web: "Web page",
     embed: "Embed", html: "Generated page", "ui-spec": "UI spec", trace: "Trace" } as Record<string, string>)[k] ?? `Plugin · ${k.slice(7)}`

export const sourceMeta: Record<ViewRef["source"], { label: string; icon: LucideIcon }> = {
  input: { label: "input", icon: Upload },
  artifact: { label: "artifact", icon: Package },
  url: { label: "url", icon: Link2 },
  citation: { label: "citation", icon: Quote },
  tool: { label: "tool result", icon: Wrench },
  run: { label: "run", icon: Play },
}

const csvCell = (c: string | number) => (/[",]/.test(String(c)) ? `"${String(c).replace(/"/g, '""')}"` : String(c))

/** Serialises any tabular or textual view to plain text (for compare and download). */
export function viewToText(v: ViewRef): string | undefined {
  if (v.table) return [v.table.columns, ...v.table.rows].map((r) => r.map(csvCell).join(",")).join("\n")
  if (v.markdown) return v.markdown.trim()
  if (v.text) return v.text.body
  if (v.json !== undefined) return JSON.stringify(v.json, null, 2)
  if (v.diff) return v.diff.after
  if (v.pages) return v.pages.map((p) => p.join("\n")).join("\n\n")
  return undefined
}
