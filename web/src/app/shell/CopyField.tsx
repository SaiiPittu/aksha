import { useEffect, useState } from "react"
import { Check, Copy } from "lucide-react"
import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"

/** A read-only value with a copy button (invite links, a new API token). */
export function CopyField({ value, label, className }: { value: string; label: string; className?: string }) {
  const [copied, setCopied] = useState(false)
  useEffect(() => { if (!copied) return; const t = setTimeout(() => setCopied(false), 1600); return () => clearTimeout(t) }, [copied])
  const copy = async () => {
    try { await navigator.clipboard.writeText(value); setCopied(true) } catch { setCopied(false) }
  }
  return (
    <div className={cn("flex min-w-0 items-center gap-1.5", className)}>
      <input readOnly value={value} aria-label={label} onFocus={(e) => e.currentTarget.select()}
        className="h-7 min-w-0 flex-1 rounded-md border bg-muted/40 px-2 font-mono text-[12px] outline-none focus:ring-2 focus:ring-ring/40" />
      <Button size="xs" variant="outline" onClick={() => void copy()} aria-label={`Copy ${label}`}>
        {copied ? <Check className="text-status-ok" /> : <Copy />}{copied ? "Copied" : "Copy"}
      </Button>
    </div>
  )
}
