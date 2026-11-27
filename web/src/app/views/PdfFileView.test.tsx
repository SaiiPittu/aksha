import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { render, screen, waitFor } from "@testing-library/react"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import type { ViewRef } from "@/features/session/types"
import { documentUrl, useLoadedView } from "./load"
import { ViewRenderer } from "./ViewRenderer"

// The server sends every document as a sandboxed attachment, so a frame pointed at the
// document URL would download it or stay blank: the panel frames a blob: URL instead.

const PDF = new TextEncoder().encode("%PDF-1.4\n%%EOF\n")
const view: ViewRef = { id: "input:spec.pdf@v1", kind: "pdf", title: "spec.pdf", source: "input", docKey: "D-1", version: "v1", versions: ["v1"] }

function Panel({ v }: { v: ViewRef }) {
  const loaded = useLoadedView(v)
  return loaded ? <ViewRenderer view={loaded.view} /> : null
}

const show = () => render(
  <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><Panel v={view} /></QueryClientProvider>,
)

let fetchFn: ReturnType<typeof vi.fn>
let created: Blob[]
const revoke = vi.fn()

beforeEach(() => {
  created = []
  revoke.mockReset()
  fetchFn = vi.fn(async () => new Response(PDF, { status: 200, headers: { "Content-Type": "application/octet-stream", "Content-Disposition": "attachment" } }))
  vi.stubGlobal("fetch", fetchFn)
  vi.spyOn(URL, "createObjectURL").mockImplementation((b) => { created.push(b as Blob); return `blob:http://localhost/pdf-${created.length}` })
  vi.spyOn(URL, "revokeObjectURL").mockImplementation(revoke)
})
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks() })

describe("a stored PDF in the view panel", () => {
  it("frames a blob: URL of the fetched bytes, typed application/pdf, never the document URL", async () => {
    show()
    const frame = await screen.findByTitle("spec.pdf")
    const src = frame.getAttribute("src") ?? ""
    expect(src.startsWith("blob:")).toBe(true)
    expect(src).not.toContain("/documents/")
    expect(frame.hasAttribute("sandbox")).toBe(false) // the browser's PDF viewer does not run in a sandboxed frame
    expect(document.querySelector(`iframe[src="${documentUrl("D-1", 1)}"]`)).toBeNull()
    expect(fetchFn).toHaveBeenCalledTimes(1)
    const [url, init] = fetchFn.mock.calls[0] as [string, RequestInit]
    expect(url).toBe(documentUrl("D-1", 1))
    expect(init.credentials).toBe("include")
    expect(created).toHaveLength(1)
    expect(created[0].type).toBe("application/pdf")
    expect(Array.from(new Uint8Array(await created[0].arrayBuffer()))).toEqual(Array.from(PDF))
  })

  it("revokes the object URL when the view unmounts", async () => {
    const { unmount } = show()
    const src = (await screen.findByTitle("spec.pdf")).getAttribute("src")
    expect(revoke).not.toHaveBeenCalled()
    unmount()
    await waitFor(() => expect(revoke).toHaveBeenCalledWith(src))
  })

  it("says so when the bytes cannot be read", async () => {
    fetchFn.mockImplementation(async () => new Response(JSON.stringify({ error: { code: "not_found", message: "no document D-1", details: null } }), { status: 404 }))
    show()
    expect(await screen.findByRole("alert")).toHaveTextContent("no document D-1")
    expect(screen.queryByTitle("spec.pdf")).toBeNull()
  })
})
