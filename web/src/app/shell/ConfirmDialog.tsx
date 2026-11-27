import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"

/** A yes-or-no question before an action that is hard to undo. */
export function ConfirmDialog({ open, title, body, confirm, destructive, onConfirm, onCancel }: {
  open: boolean; title: string; body: React.ReactNode; confirm: string; destructive?: boolean
  onConfirm: () => void; onCancel: () => void
}) {
  return (
    <Dialog open={open} onOpenChange={(o) => { if (!o) onCancel() }}>
      <DialogContent className="sm:max-w-sm">
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>{body}</DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button variant="ghost" onClick={onCancel}>Cancel</Button>
          <Button variant={destructive ? "destructive" : "default"} onClick={onConfirm}>{confirm}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
