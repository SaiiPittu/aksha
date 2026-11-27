/**
 * The policy agent-generated HTML runs under: no fetch, no script, no outside resource;
 * inline styles and data: images only. A `sandbox` frame without `allow-same-origin`
 * keeps the page away from the app's cookies, but on its own it could still reach the
 * network, so the policy is put into the document itself.
 */
export const AGENT_HTML_CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src data:"

const META = `<meta http-equiv="Content-Security-Policy" content="${AGENT_HTML_CSP}">`
const DOCTYPE = /^\s*<!doctype[^>]*>/i

/**
 * `html` with the policy's meta tag first, before anything the agent wrote (after a
 * leading doctype only), so it applies to every element that follows.
 */
export function withAgentCsp(html: string): string {
  const doctype = DOCTYPE.exec(html)
  return doctype ? doctype[0] + META + html.slice(doctype[0].length) : META + html
}
