import { Fragment } from "react"
import type { ReactNode } from "react"
import { cn } from "@/lib/utils"

// A small, dependency-free Markdown renderer: headings, paragraphs, lists,
// tables, bold, inline code and citation anchors like [2]. Styled with theme tokens only.

type Block =
  | { t: "h"; level: number; text: string; id: string }
  | { t: "p"; text: string }
  | { t: "ul" | "ol"; items: string[] }
  | { t: "table"; head: string[]; rows: string[][] }
  | { t: "code"; text: string }
  | { t: "hr" }

const slug = (s: string) => s.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "")
const cells = (line: string) => line.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim())

export function parseMarkdown(src: string): Block[] {
  const lines = src.split("\n"), out: Block[] = []
  for (let i = 0; i < lines.length; ) {
    const l = lines[i]
    if (!l.trim()) { i++; continue }
    const h = l.match(/^(#{1,4})\s+(.*)/)
    if (h) { out.push({ t: "h", level: h[1].length, text: h[2], id: slug(h[2]) }); i++; continue }
    if (/^---+\s*$/.test(l)) { out.push({ t: "hr" }); i++; continue }
    if (l.startsWith("```")) {
      const body: string[] = []; i++
      while (i < lines.length && !lines[i].startsWith("```")) body.push(lines[i++])
      i++; out.push({ t: "code", text: body.join("\n") }); continue
    }
    if (l.trim().startsWith("|") && lines[i + 1]?.match(/^\s*\|?\s*:?-{2,}/)) {
      const head = cells(l), rows: string[][] = []; i += 2
      while (i < lines.length && lines[i].trim().startsWith("|")) rows.push(cells(lines[i++]))
      out.push({ t: "table", head, rows }); continue
    }
    const li = l.match(/^\s*([-*]|\d+\.)\s+/)
    if (li) {
      const ordered = /\d/.test(li[1]), items: string[] = []
      while (i < lines.length && lines[i].match(/^\s*([-*]|\d+\.)\s+/)) items.push(lines[i++].replace(/^\s*([-*]|\d+\.)\s+/, ""))
      out.push({ t: ordered ? "ol" : "ul", items }); continue
    }
    const para: string[] = []
    while (i < lines.length && lines[i].trim() && !/^(#|\||```|\s*[-*]\s|\s*\d+\.\s)/.test(lines[i])) para.push(lines[i++])
    out.push({ t: "p", text: para.join(" ") })
  }
  return out
}

export function Inline({ text }: { text: string }) {
  const parts = text.split(/(\*\*[^*]+\*\*|`[^`]+`|\[\d+\])/g)
  return (
    <>
      {parts.map((p, i) => {
        if (/^\*\*.*\*\*$/.test(p)) return <strong key={i} className="font-semibold text-foreground">{p.slice(2, -2)}</strong>
        if (/^`.*`$/.test(p)) return <code key={i} className="rounded bg-muted px-1 py-0.5 font-mono text-[0.9em]">{p.slice(1, -1)}</code>
        if (/^\[\d+\]$/.test(p)) return (
          <sup key={i}><a href="#" onClick={(e) => e.preventDefault()}
            className="ml-0.5 rounded bg-muted px-1 font-mono text-[10.5px] text-muted-foreground no-underline hover:bg-accent hover:text-foreground">{p.slice(1, -1)}</a></sup>
        )
        return <Fragment key={i}>{p}</Fragment>
      })}
    </>
  )
}

const hClass: Record<number, string> = {
  1: "mt-0 mb-4 text-[22px] font-semibold tracking-tight",
  2: "mt-8 mb-3 border-b pb-1.5 text-[16px] font-semibold",
  3: "mt-6 mb-2 text-[14.5px] font-semibold",
  4: "mt-4 mb-2 text-[13.5px] font-semibold text-muted-foreground",
}

export function MarkdownView({ markdown, outline }: { markdown: string; outline?: boolean }) {
  const blocks = parseMarkdown(markdown)
  const heads = blocks.filter((b): b is Extract<Block, { t: "h" }> => b.t === "h" && b.level <= 3)
  return (
    <div className="@container h-full overflow-auto">
      <div className="mx-auto flex max-w-[1040px] gap-10 px-8 py-8">
        <article className="min-w-0 max-w-[720px] flex-1 text-[14px] leading-[1.65] text-foreground/90">
          {blocks.map((b, i) => <MdBlock key={i} b={b} />)}
        </article>
        {outline && heads.length > 1 && (
          <nav className="sticky top-0 hidden w-48 shrink-0 self-start text-[12.5px] @3xl:block">
            <div className="mb-2 text-[11px] font-medium tracking-wide text-muted-foreground uppercase">On this page</div>
            <ul className="space-y-1.5 border-l">
              {heads.map((h, i) => (
                <li key={h.id} className={cn("-ml-px border-l pl-3", i === 0 ? "border-foreground text-foreground" : "border-transparent text-muted-foreground", h.level === 3 && "pl-6")}>
                  <a href={`#${h.id}`} onClick={(e) => e.preventDefault()} className="hover:text-foreground">{h.text}</a>
                </li>
              ))}
            </ul>
          </nav>
        )}
      </div>
    </div>
  )
}

function MdBlock({ b }: { b: Block }): ReactNode {
  switch (b.t) {
    case "h": {
      const H = `h${b.level}` as "h1"
      return <H id={b.id} className={hClass[b.level]}><Inline text={b.text} /></H>
    }
    case "p": return <p className="my-3"><Inline text={b.text} /></p>
    case "ul": return <ul className="my-3 list-disc space-y-1.5 pl-5 marker:text-muted-foreground">{b.items.map((t, i) => <li key={i}><Inline text={t} /></li>)}</ul>
    case "ol": return <ol className="my-3 list-decimal space-y-1.5 pl-5 marker:text-muted-foreground">{b.items.map((t, i) => <li key={i}><Inline text={t} /></li>)}</ol>
    case "hr": return <hr className="my-6" />
    case "code": return <pre className="my-4 overflow-auto rounded-md border bg-muted/50 p-3 font-mono text-[12.5px]">{b.text}</pre>
    case "table": return (
      <div className="my-4 overflow-hidden rounded-lg border">
        <table className="w-full border-collapse text-[13px]">
          <thead className="bg-muted/60"><tr>{b.head.map((c, i) => <th key={i} className="border-b px-3 py-2 text-left font-medium">{c}</th>)}</tr></thead>
          <tbody>{b.rows.map((r, i) => (
            <tr key={i} className="border-b last:border-0">{r.map((c, j) => <td key={j} className="px-3 py-2 tabular-nums"><Inline text={c} /></td>)}</tr>
          ))}</tbody>
        </table>
      </div>
    )
  }
}
