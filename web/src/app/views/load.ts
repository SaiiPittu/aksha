// Fills a ViewRef with its content when the panel opens it: artifacts from
// GET /artifacts/{ref}, uploaded documents from GET /documents/{doc_key}?version=N.
// The content's media type picks the renderer: JSON (a tree, or a table for a list of
// records), Markdown, CSV as a table, plain text.

import { API_BASE, useArtifact, useArtifactDiff, useDocument, type ArtifactDetail } from "@/api"
import type { ArtifactCitation, Freshness, ViewRef } from "@/features/session/types"

const enc = encodeURIComponent

export const vNum = (v?: string) => {
  const n = Number((v ?? "").replace(/^@?v/, ""))
  return Number.isFinite(n) && n > 0 ? n : undefined
}

/** The ref of `view` at another version (`kind/key@v1`). */
export function refAt(view: ViewRef, version?: string): string | undefined {
  if (!view.ref) return undefined
  const slot = view.ref.split("@")[0]
  return version ? `${slot}@${version.startsWith("v") ? version : `v${version}`}` : view.ref
}

type Cell = string | number

/** A list of flat-ish records → a table; anything else → undefined. */
export function recordsTable(v: unknown): { columns: string[]; rows: Cell[][] } | undefined {
  if (!Array.isArray(v) || v.length === 0) return undefined
  if (!v.every((r) => r && typeof r === "object" && !Array.isArray(r))) return undefined
  const columns = [...new Set(v.flatMap((r) => Object.keys(r as object)))]
  if (columns.length === 0 || columns.length > 40) return undefined
  const cell = (x: unknown): Cell => (typeof x === "number" ? x : x == null ? "" : typeof x === "string" ? x : JSON.stringify(x))
  return { columns, rows: v.map((r) => columns.map((c) => cell((r as Record<string, unknown>)[c]))) }
}

export function parseCsv(text: string): { columns: string[]; rows: Cell[][] } {
  const lines: string[][] = []
  let row: string[] = [], cur = "", q = false
  for (let i = 0; i < text.length; i++) {
    const ch = text[i]
    if (q) {
      if (ch === '"' && text[i + 1] === '"') { cur += '"'; i++ }
      else if (ch === '"') q = false
      else cur += ch
    } else if (ch === '"') q = true
    else if (ch === ",") { row.push(cur); cur = "" }
    else if (ch === "\n" || ch === "\r") {
      if (ch === "\r" && text[i + 1] === "\n") i++
      row.push(cur); lines.push(row); row = []; cur = ""
    } else cur += ch
  }
  if (cur || row.length) { row.push(cur); lines.push(row) }
  const [columns = [], ...rows] = lines.filter((l) => l.some((c) => c !== ""))
  return { columns, rows }
}

/** Content plus its media type → the fields a renderer reads. */
export function contentView(base: ViewRef, content: unknown, media?: string): ViewRef {
  const m = (media ?? base.mediaType ?? "").toLowerCase()
  const title = base.title.toLowerCase()
  if (typeof content === "string") {
    if (m === "text/csv" || title.endsWith(".csv")) return { ...base, kind: "table", table: parseCsv(content) }
    if (m === "text/markdown" || title.endsWith(".md")) return { ...base, kind: "markdown", markdown: content }
    if (m.includes("json") || title.endsWith(".json")) {
      try { return contentView(base, JSON.parse(content), "application/json") } catch { /* shown as text */ }
    }
    const lang = title.endsWith(".json") ? "json" : title.endsWith(".py") ? "python" : title.endsWith(".ts") ? "typescript" : "text"
    return { ...base, kind: "text", text: { language: lang, body: content } }
  }
  const t = recordsTable(content)
  if (t) return { ...base, kind: "table", table: t, json: content }
  return { ...base, kind: "json", json: content }
}

/** The text of a content value, for diffs. */
export const contentText = (c: unknown) => (typeof c === "string" ? c : JSON.stringify(c, null, 2) ?? "")

export const documentUrl = (docKey: string, version?: number) =>
  `${API_BASE}/documents/${enc(docKey)}${version ? `?version=${version}` : ""}`

function fromArtifact(base: ViewRef, d: ArtifactDetail): ViewRef {
  const citations: ArtifactCitation[] = d.citations.map((c) => ({
    pointer: c.pointer ?? undefined, quote: c.quote ?? undefined, target: c.target ?? undefined, form: c.form ?? undefined, recordId: c.record_id ?? undefined,
  }))
  return contentView({
    ...base,
    versions: d.versions.length ? d.versions.map((v) => `v${v}`) : base.versions,
    mediaType: d.media_type ?? base.mediaType,
    freshness: (d.freshness ?? undefined) as Freshness | undefined,
    citations: citations.length ? citations : base.citations,
  }, d.content, d.media_type ?? undefined)
}

export interface Loaded { view: ViewRef; loading: boolean; error?: { code: string; message: string }; detail?: ArtifactDetail }

const errOf = (e: unknown) => (e ? { code: (e as { code?: string }).code ?? "error", message: (e as Error).message ?? String(e) } : undefined)

/** The view filled with its content at `version` (default: the view's own). */
export function useLoadedView(view: ViewRef | undefined, version?: string): Loaded | undefined {
  const isArtifact = !!view?.ref && view.source === "artifact" && view.kind !== "diff"
  const isDoc = !!view?.docKey && view.source === "input"
  const binary = isDoc && (view?.kind === "image" || view?.kind === "pdf")
  const art = useArtifact(isArtifact ? refAt(view!, version) : undefined)
  const doc = useDocument(isDoc && !binary ? view!.docKey : undefined, vNum(version ?? view?.version))
  if (!view) return undefined
  if (isArtifact) {
    if (art.data) return { view: fromArtifact({ ...view, version: version ?? view.version }, art.data), loading: false, detail: art.data }
    return { view, loading: art.isLoading, error: errOf(art.error) }
  }
  if (binary) {
    const url = documentUrl(view.docKey!, vNum(version ?? view.version))
    // An image reads `url` with <img>; a PDF's `url` is only fetched (PdfFileView), never framed.
    return { view: { ...view, url }, loading: false }
  }
  if (isDoc) {
    if (doc.data) return { view: contentView({ ...view, version: version ?? view.version }, doc.data.text, doc.data.mediaType || view.mediaType), loading: false }
    return { view, loading: doc.isLoading, error: errOf(doc.error) }
  }
  return { view, loading: false }
}

/** Before and after texts for "Diff with…": the artifact diff read, or two document versions. */
export function useCompare(view: ViewRef | undefined, shown?: string, against?: string): { before?: string; after?: string; loading: boolean } {
  const isArtifact = !!view?.ref && view.source === "artifact" && !!against
  const isDoc = !!view?.docKey && view.source === "input" && !!against
  const diff = useArtifactDiff(isArtifact ? refAt(view!, shown) : undefined, isArtifact ? against : undefined)
  const a = useDocument(isDoc ? view!.docKey : undefined, vNum(against))
  const b = useDocument(isDoc ? view!.docKey : undefined, vNum(shown ?? view?.version))
  if (isArtifact) return { before: diff.data ? contentText(diff.data.before) : undefined, after: diff.data ? contentText(diff.data.after) : undefined, loading: diff.isLoading }
  if (isDoc) return { before: a.data?.text, after: b.data?.text, loading: a.isLoading || b.isLoading }
  return { loading: false }
}
