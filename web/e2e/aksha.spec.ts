// The web app end to end against a real `aksha serve` (global-setup.ts): local mode, the
// whole patent conversation through the UI, the re-upload rebuild, then accounts and roles.
// Serial: each test builds on the server state the one before left.
import path from "node:path"
import { changeSet, expect, mailbox, shot, test, worlds } from "./fixtures.ts"

test.describe.configure({ mode: "serial" })

const PROMPT = "Assess the four invention candidates and prepare the chosen one for counsel."
const GAP = "0.5 m from the vent edge, so its air flow stays outside the footprint."
let sessionUrl = ""

test("local mode: the app opens without signing in and says so", async ({ page }) => {
  await page.goto("/")
  await expect(page.getByText("Local mode: no accounts yet")).toBeVisible()
  await expect(page.getByRole("button", { name: "Claim this workspace" })).toBeVisible()
  await expect(page.getByRole("textbox", { name: "Message" })).toBeVisible()
  await shot(page, "01-local-banner")
})

test("the patent conversation, through the UI only, ends complete and delivers the packet", async ({ page }) => {
  await page.goto("/")
  await page.getByRole("combobox", { name: "Agent" }).click()
  await page.getByRole("option", { name: /^patent/ }).click()
  await page.getByTestId("attach-folder-input").setInputFiles(path.join(worlds, "stillroom", "sources"))
  await expect(page.getByText("22 files · 22 stored")).toBeVisible()
  await page.getByRole("button", { name: "Show all 22" }).click()
  const chips = page.getByRole("list", { name: "Attachments" }).getByRole("listitem")
  await expect(chips).toHaveCount(22)
  await expect(chips.filter({ hasText: "notes/C1-self-calibration.md" })).toContainText("v1")
  await page.getByRole("button", { name: "Show fewer" }).click()
  await page.getByRole("textbox", { name: "Message" }).fill(PROMPT)
  await shot(page, "02-new-chat-attachments")
  await page.getByRole("button", { name: "Send" }).click()

  await expect(page).toHaveURL(/\/s\/S-[0-9a-f]{12}/)
  sessionUrl = new URL(page.url()).pathname
  await expect(page.getByText(PROMPT).first()).toBeVisible()
  await expect(page.locator("header").getByText("R-0001")).toBeVisible()
  await shot(page, "03-run-streaming")

  // The gap question, answered in its card.
  const question = page.locator("[data-interrupt-kind=input]").filter({ hasText: "Waiting on you" })
  await expect(question).toBeVisible({ timeout: 120_000 })
  await expect(question).toContainText("vent")
  await page.getByRole("status", { name: "Waiting on you" }).getByRole("button", { name: "Jump to it" }).click()
  await expect(question).toBeInViewport()
  await shot(page, "04-question-card")
  await question.getByRole("textbox").fill(GAP)
  await question.getByRole("button", { name: "Submit" }).click()

  // The choice: C1.
  const choice = page.locator("[data-interrupt-kind=choice]").filter({ hasText: "Waiting on you" })
  await expect(choice).toBeVisible({ timeout: 120_000 })
  await expect(choice.getByRole("radio")).toHaveCount(4)
  await choice.scrollIntoViewIfNeeded()
  await shot(page, "05-choice-card")
  await choice.getByRole("radio", { name: /C1/ }).click()
  await choice.getByRole("button", { name: "Submit" }).click()

  // The approval: the exact tool, the recipient and the full signature.
  const approval = page.locator("[data-interrupt-kind=approval]").filter({ hasText: "Needs your approval" })
  await expect(approval).toBeVisible({ timeout: 120_000 })
  await expect(approval.locator("code").first()).toHaveText("send_packet")
  await expect(approval).toContainText("counsel-primary")
  await approval.scrollIntoViewIfNeeded()
  const signature = (await approval.getByLabel("Signature").innerText()).trim()
  expect(signature.length).toBeGreaterThan(40)
  await shot(page, "06-approval-card")
  // The inbox, in another tab, lists the same approval.
  const inbox = await page.context().newPage()
  await inbox.goto("/inbox")
  await expect(inbox.getByRole("row").filter({ hasText: "A-0001" })).toBeVisible()
  await shot(inbox, "06b-inbox")
  await inbox.close()
  const before = mailbox().length
  await approval.getByRole("button", { name: "Approve and send" }).click()

  await expect(page.locator("header").getByText(/complete|Done/i)).toBeVisible({ timeout: 120_000 })
  await expect.poll(() => mailbox().length, { timeout: 30_000 }).toBeGreaterThan(before)
  expect(mailbox().at(-1)?.request).toEqual({ packet: "patent.packet/C1@v1", recipient: "counsel-primary" })

  // An artifact from its chip into the right panel, its versions, then Lineage.
  const chip = page.locator("[data-opens-view]").filter({ hasText: "patent.packet" }).first()
  await chip.click()
  const panel = page.getByRole("complementary", { name: "View panel" })
  await expect(panel).toBeVisible()
  await expect(panel.getByRole("heading", { name: /patent\.packet\/C1/ })).toBeVisible()
  await expect(panel.getByText(/counsel/i).first()).toBeVisible()
  await panel.getByRole("button", { name: "Pin panel" }).click()
  await expect(panel).toHaveAttribute("data-mode", "pinned")
  await shot(page, "07-artifact-panel")
  await panel.getByRole("tab", { name: "Lineage" }).click()
  await expect(panel.getByText("patent.extraction/C1@v1").first()).toBeVisible()
  await shot(page, "08-lineage")
})

test("re-uploading the change set rebuilds live in the same thread, through a review and a fresh approval", async ({ page }) => {
  expect(sessionUrl).not.toBe("")
  await page.goto(sessionUrl)
  await expect(page.locator("[data-interrupt-kind=approval]").first()).toBeVisible()
  await page.getByTestId("attach-folder-input").setInputFiles(changeSet())
  const chips = page.getByRole("list", { name: "Attachments" }).getByRole("listitem")
  await expect(chips).toHaveCount(3)
  await expect(chips.filter({ hasText: "disclosures/history.csv" })).toContainText("changed (v1 → v2)")
  await expect(chips.filter({ hasText: "publications/SYN-PUB-0011.json" })).toContainText("v1")

  // The change notice and the rebuild run, live in this thread.
  await expect(page.getByText(/Inputs changed: .*disclosures\/history\.csv/).first()).toBeVisible()
  await expect(page.getByText(/Rebuilding after an input change/).first()).toBeVisible({ timeout: 120_000 })
  await shot(page, "09-reupload-rebuild")

  const review = page.locator("[data-interrupt-kind=review]").filter({ hasText: "Waiting on you" })
  await expect(review).toBeVisible({ timeout: 180_000 })
  await expect(review).toContainText("patent.packet/C1")
  await review.scrollIntoViewIfNeeded()
  await shot(page, "10-review-card")
  await review.getByRole("button", { name: /^Accept/ }).click()
  await expect(page.locator("[data-interrupt-kind=review][data-interrupt-state=resolved]")).toBeVisible()

  const approval = page.locator("[data-interrupt-kind=approval]").filter({ hasText: "Needs your approval" })
  await expect(approval).toBeVisible({ timeout: 180_000 })
  await expect(approval).toContainText("patent.packet/C1@v2")
  const before = mailbox().length
  await approval.getByRole("button", { name: "Approve and send" }).click()
  await expect.poll(() => mailbox().length, { timeout: 60_000 }).toBeGreaterThan(before)
  expect(mailbox().at(-1)?.request).toEqual({ packet: "patent.packet/C1@v2", recipient: "counsel-primary" })

  // The packet now has two versions: switch between them in the panel.
  await page.locator("[data-opens-view]").filter({ hasText: "patent.packet/C1" }).last().click()
  const panel = page.getByRole("complementary", { name: "View panel" })
  await expect(panel).toBeVisible()
  await panel.getByRole("button", { name: /^v2/ }).click()
  await page.getByRole("menuitemradio", { name: /^v1/ }).click()
  await expect(panel.getByRole("button", { name: /^v1/ })).toBeVisible()
  await shot(page, "11-versions")

  // Dark mode follows the system preference.
  await page.getByRole("button", { name: "Close panel" }).click()
  await page.emulateMedia({ colorScheme: "dark" })
  await expect(page.locator("html")).toHaveClass(/dark/)
  await shot(page, "17-chat-dark")
  await page.emulateMedia({ colorScheme: "light" })
})

test("an uploaded PDF opens in the view panel from a blob: URL, and nothing is downloaded", async ({ page }) => {
  // The server sends every document as a sandboxed attachment: the panel must fetch the
  // bytes and frame an object URL, never the document URL (which would download it).
  const downloads: string[] = []
  page.on("download", (d) => downloads.push(d.suggestedFilename()))
  await page.goto("/")
  await page.getByTestId("attach-files-input").setInputFiles(path.join(import.meta.dirname, "files", "tiny.pdf"))
  const chip = page.getByRole("list", { name: "Attachments" }).getByRole("listitem").filter({ hasText: "tiny.pdf" })
  await expect(chip).toContainText("v1")
  await page.goto("/files")
  await page.getByRole("tab", { name: /Inputs/ }).click()
  await page.getByRole("row").filter({ hasText: "tiny.pdf" }).click()
  const panel = page.getByRole("complementary", { name: "View panel" })
  const frame = panel.locator('iframe[title="tiny.pdf"]')
  await expect(frame).toHaveAttribute("src", /^blob:/)
  expect(await panel.locator("iframe[src*='/documents/']").count()).toBe(0)
  await page.waitForTimeout(1500) // a framed attachment would have started its download by now
  await shot(page, "12-pdf-panel")
  expect(downloads).toEqual([])
})

const OWNER = { name: "Olive Owner", email: "owner@example.test", password: "e2e-only-Pass-2026!", org: "Example Lab" }
const VIEWER = { name: "Vic Viewer", email: "viewer@example.test", password: "e2e-only-View-2026!" }

test("claim the workspace, invite a viewer, and the viewer is read only", async ({ page, browser, baseURL }) => {
  await page.goto("/")
  await page.getByRole("button", { name: "Claim this workspace" }).click()
  const claim = page.getByRole("form", { name: "Claim workspace" })
  await claim.getByLabel("Name", { exact: true }).fill(OWNER.name)
  await claim.getByLabel("Email").fill(OWNER.email)
  await claim.getByRole("textbox", { name: "Password" }).fill(OWNER.password)
  await claim.getByLabel("Organisation name").fill(OWNER.org)
  await claim.getByRole("button", { name: "Claim workspace" }).click()
  await expect(page.getByText("Local mode: no accounts yet")).toHaveCount(0)
  await expect(page.getByText(OWNER.org).first()).toBeVisible()

  // Everything done in local mode is kept: the session is still listed.
  await page.goto("/sessions")
  await expect(page.getByText(/Assess the four invention candidates/).first()).toBeVisible()
  await shot(page, "12-sessions")

  await page.goto("/settings/invites")
  const form = page.getByRole("form", { name: "Invite people" })
  await form.getByLabel("Add emails").fill(VIEWER.email)
  await form.getByLabel("Add emails").press("Enter")
  await form.getByRole("combobox", { name: "Role for new invites" }).click()
  await page.getByRole("option", { name: "Viewer" }).click()
  await form.getByRole("button", { name: /^Create invite/ }).click()
  const link = await form.getByRole("textbox", { name: `invite link for ${VIEWER.email}` }).inputValue()
  expect(link).toContain("/invite/")
  await shot(page, "14-invites")

  // The viewer accepts in a second browser context (their own cookie). Signed out, the
  // app now asks for sign in: local mode ended with the claim.
  const ctx = await browser.newContext({ baseURL, viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1.5 })
  const viewer = await ctx.newPage()
  await viewer.goto("/")
  await expect(viewer).toHaveURL(/\/sign-in/)
  await expect(viewer.getByRole("form", { name: "Sign in" })).toBeVisible()
  await shot(viewer, "13-sign-in")
  await viewer.goto(link)
  const accept = viewer.getByRole("form", { name: "Accept invite" })
  await accept.getByLabel("Name", { exact: true }).fill(VIEWER.name)
  await accept.getByRole("textbox", { name: "Password" }).fill(VIEWER.password)
  await accept.getByRole("button", { name: /^Join/ }).click()
  await expect(viewer.getByText(/read-only access \(Viewer\)/)).toBeVisible()
  await expect(viewer.getByRole("textbox", { name: "Message" })).toHaveCount(0)
  await expect(viewer.getByRole("link", { name: /New chat/ })).toHaveCount(0)
  await shot(viewer, "15-viewer-read-only")
  // A command the UI hides is refused by the server too.
  const refused = await viewer.evaluate(async () => {
    const me = await (await fetch("/api/v1/auth/me")).json() as { csrf: string }
    const res = await fetch("/api/v1/sessions/S-000000000000/messages", {
      method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": me.csrf },
      body: JSON.stringify({ text: "hello", command_id: "c-viewer-try" }),
    })
    return { status: res.status, body: await res.json() as { error?: { code?: string } } }
  })
  expect(refused.status).toBe(403)
  expect(refused.body.error?.code).toBe("forbidden_role")
  await ctx.close()

  // The owner sees the viewer as a member with the Viewer role.
  await page.goto("/settings/members")
  await expect(page.getByRole("row").filter({ hasText: VIEWER.email })).toContainText("Viewer")
  await shot(page, "16-members")
})
