// Shared by the specs: the server global-setup.ts started, its AKSHA_HOME, the bundled
// world's files, and screenshots for the maintainer (only when E2E_SHOTS names a folder).
import { test as base, expect, type Page } from "@playwright/test"
import { mkdirSync, readdirSync, readFileSync, writeFileSync } from "node:fs"
import path from "node:path"

export const repo = path.resolve(import.meta.dirname, "..", "..")
export const worlds = path.join(repo, "src", "aksha", "data", "worlds")
export const home = () => process.env.E2E_AKSHA_HOME!

export const test = base.extend({
  // Playwright reads fixture dependencies from a destructuring pattern, so it stays empty.
  // eslint-disable-next-line no-empty-pattern
  baseURL: async ({}, provide) => { await provide(process.env.E2E_BASE_URL) },
})
export { expect }

/** Every file under `dir`, as paths relative to it, sorted. */
export function filesUnder(dir: string, rel = ""): string[] {
  const out: string[] = []
  for (const e of readdirSync(path.join(dir, rel), { withFileTypes: true })) {
    const r = rel ? `${rel}/${e.name}` : e.name
    if (e.isDirectory()) out.push(...filesUnder(dir, r))
    else out.push(r)
  }
  return out.sort()
}

/** The stillroom-v2 change set as a folder: one new publication and two moved dates. */
export function changeSet(): string {
  const v1 = path.join(worlds, "stillroom", "sources")
  const v2 = path.join(worlds, "stillroom-v2", "sources")
  const out = path.join(home(), "e2e-change-set")
  const put = (rel: string, text: string) => {
    mkdirSync(path.dirname(path.join(out, rel)), { recursive: true })
    writeFileSync(path.join(out, rel), text)
  }
  put("publications/SYN-PUB-0011.json", readFileSync(path.join(v2, "publications/SYN-PUB-0011.json"), "utf8"))
  const email = readFileSync(path.join(v1, "disclosures/consultant-email.eml.txt"), "utf8")
  const moved = email.replace("Date: Mon, 05 Jan 2026 16:42:10 +0100", "Date: Fri, 21 Nov 2025 16:42:10 +0100")
  if (moved === email) throw new Error("the change set's email date was not found")
  put("disclosures/consultant-email.eml.txt", moved)
  const history = readFileSync(path.join(v1, "disclosures/history.csv"), "utf8")
  const moved2 = history.replace("DISC-05,2026-01-05,", "DISC-05,2025-11-21,")
  if (moved2 === history) throw new Error("the change set's history date was not found")
  put("disclosures/history.csv", moved2)
  return out
}

/** The deliveries in the counsel mailbox (the mock system the packet goes to), oldest first. */
export interface Delivery { message_id: string; request: { packet?: string; recipient?: string } }
export function mailbox(): Delivery[] {
  try { return Object.values(JSON.parse(readFileSync(path.join(home(), "mailbox", "mailbox.json"), "utf8")) as Record<string, Delivery>) } catch { return [] }
}

/** A 1440x900 screenshot at device scale 1.5, when E2E_SHOTS names a folder. */
export async function shot(page: Page, name: string) {
  const dir = process.env.E2E_SHOTS
  if (!dir) return
  mkdirSync(dir, { recursive: true })
  await page.waitForTimeout(250)
  await page.screenshot({ path: path.join(dir, `${name}.png`) })
}
