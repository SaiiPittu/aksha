import { describe, expect, it } from "vitest"
import { AGENT_HTML_CSP, withAgentCsp } from "./frameCsp"

const META = `<meta http-equiv="Content-Security-Policy" content="${AGENT_HTML_CSP}">`

describe("withAgentCsp", () => {
  it("puts the policy before anything the agent wrote", () => {
    const html = "<script>fetch('https://example.org')</script><p>hi</p>"
    expect(withAgentCsp(html)).toBe(META + html)
  })
  it("keeps a leading doctype first, so the page stays in standards mode", () => {
    expect(withAgentCsp("<!DOCTYPE html><html><head><title>t</title></head></html>"))
      .toBe(`<!DOCTYPE html>${META}<html><head><title>t</title></head></html>`)
  })
  it("never lets a script before <head> run ahead of the policy", () => {
    const out = withAgentCsp("<!doctype html><script>x()</script><head></head>")
    expect(out.indexOf(META)).toBeLessThan(out.indexOf("<script>"))
  })
  it("names no network source", () => {
    expect(AGENT_HTML_CSP).toContain("default-src 'none'")
    expect(AGENT_HTML_CSP).not.toMatch(/connect-src|script-src|https?:/)
  })
})
