// Builds the web app if its build is missing or older than its sources, then starts a real
// `aksha serve` (fake models, the mock counsel mailbox) on a free loopback port with an
// isolated AKSHA_HOME, serving the build. Returns the teardown.
import { spawn, spawnSync, type ChildProcess } from "node:child_process"
import { existsSync, mkdtempSync, readdirSync, rmSync, statSync } from "node:fs"
import { createServer } from "node:net"
import { tmpdir } from "node:os"
import path from "node:path"

const web = path.resolve(import.meta.dirname, "..")
const repo = path.resolve(web, "..")

function newest(dir: string): number {
  let t = 0
  for (const e of readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, e.name)
    t = Math.max(t, e.isDirectory() ? newest(p) : statSync(p).mtimeMs)
  }
  return t
}

function buildIfNeeded() {
  const index = path.join(web, "dist", "index.html")
  const sources = Math.max(newest(path.join(web, "src")), statSync(path.join(web, "index.html")).mtimeMs)
  if (existsSync(index) && statSync(index).mtimeMs >= sources && !process.env.E2E_REBUILD) return
  const r = spawnSync("nice", ["-n", "19", "npm", "run", "build"], { cwd: web, stdio: "inherit" })
  if (r.status !== 0) throw new Error("npm run build failed")
}

function freePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const s = createServer()
    s.unref()
    s.on("error", reject)
    s.listen(0, "127.0.0.1", () => {
      const a = s.address()
      const port = typeof a === "object" && a ? a.port : 0
      s.close(() => resolve(port))
    })
  })
}

async function waitUp(url: string, child: ChildProcess, ms = 60_000) {
  const until = Date.now() + ms
  while (Date.now() < until) {
    if (child.exitCode !== null) throw new Error(`aksha serve exited with ${child.exitCode}`)
    try { if ((await fetch(`${url}/api/v1/auth/status`)).ok) return } catch { /* not yet */ }
    await new Promise((r) => setTimeout(r, 250))
  }
  throw new Error(`aksha serve did not answer at ${url}`)
}

export default async function globalSetup() {
  buildIfNeeded()
  const home = mkdtempSync(path.join(tmpdir(), "aksha-e2e-"))
  const port = await freePort()
  const url = `http://127.0.0.1:${port}`
  const aksha = process.env.E2E_AKSHA ?? path.join(repo, ".venv", "bin", "aksha")
  const child = spawn("nice", ["-n", "19", aksha, "serve", "--host", "127.0.0.1", "--port", String(port), "--web", path.join(web, "dist")], {
    cwd: home,
    env: {
      ...process.env,
      AKSHA_HOME: home,
      AKSHA_MODE: "fake",
      AKSHA_SYSTEMS: `counsel_mail=mailbox:${path.join(home, "mailbox")}`,
      AKSHA_SERVER_URL: "",
    },
    stdio: ["ignore", "inherit", "inherit"],
  })
  await waitUp(url, child)
  process.env.E2E_BASE_URL = url
  process.env.E2E_AKSHA_HOME = home
  return async () => {
    child.kill("SIGINT")
    await new Promise((r) => { child.once("exit", r); setTimeout(r, 10_000) })
    if (child.exitCode === null) child.kill("SIGKILL")
    if (!process.env.E2E_KEEP_HOME) rmSync(home, { recursive: true, force: true })
  }
}
