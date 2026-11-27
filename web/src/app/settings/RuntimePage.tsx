import { ShieldCheck } from "lucide-react"
import { useCapabilities, useVerify } from "@/api"
import { Button } from "@/components/ui/button"
import { Spinner } from "@/components/ui/spinner"
import { ErrorNote } from "@/app/shell/ErrorNote"
import { PageBody, PageHeader } from "@/app/pages/common"
import { SettingsFrame } from "./SettingsPage"

/** The runtime's capability record (agents, commands, interrupt kinds) and the event chain check. */
export function RuntimePage() {
  const caps = useCapabilities()
  const verify = useVerify()
  return (
    <SettingsFrame active="runtime">
      <PageHeader title="Runtime" sub="What this server's runtime can do, and whether its event log is intact">
        <Button size="sm" variant="outline" disabled={verify.isPending} onClick={() => verify.mutate()}>
          {verify.isPending ? <Spinner className="size-3.5" /> : <ShieldCheck />}Verify event chain
        </Button>
      </PageHeader>
      <PageBody className="flex flex-col gap-4">
        <ErrorNote error={verify.error} />
        {verify.data !== undefined && (
          <section className="rounded-lg border border-status-ok/30 bg-status-ok-soft/50 p-3">
            <p className="mb-1 text-[13px] font-medium">Chain check</p>
            <pre className="overflow-auto font-mono text-[12px] whitespace-pre-wrap">{JSON.stringify(verify.data, null, 2)}</pre>
          </section>
        )}
        <ErrorNote error={caps.error} />
        {caps.isPending ? <div className="flex justify-center py-8"><Spinner className="size-5 text-muted-foreground" /></div> : caps.data && (
          <section className="rounded-lg border bg-card p-4">
            <h2 className="mb-2 text-[13.5px] font-semibold">Capability record</h2>
            <pre className="max-h-[60vh] overflow-auto rounded-md bg-muted/40 p-3 font-mono text-[12px]">{JSON.stringify(caps.data, null, 2)}</pre>
          </section>
        )}
      </PageBody>
    </SettingsFrame>
  )
}
