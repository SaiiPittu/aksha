"""``make demo``: change and recovery, offline, one command, fake models only.

The story, in five acts, on one owner's SQLite store (``story.py``):

1. The research agent and the patent agent run on the ``stillroom`` fixture world to
   completion. The patent agent's one send (C1's counsel-review packet) goes to a mock
   counsel mailbox after an approval.
2. The input changes to ``stillroom-v2``'s change set (``world.py``): a new publication
   family and an earlier date on the outside consultant's suggestion. One command records
   what moved.
3. Invalidation reopens only the work whose lineage reaches the change; everything else is
   kept. The patent packet is regenerated, a diff review and a fresh approval are asked,
   and the demo script answers them.
4. The send of the new packet is killed with a real ``SIGKILL`` right after the mailbox
   recorded it and before the runtime recorded the receipt; recovery asks the mailbox by
   key and finishes the run without sending again (``child.py``).
5. The event store's hash chain is verified and a short summary prints (``summary.py``).

Every answer a person would give (the research question, the inventor's gap answer, the
candidate choice, the diff review, both approvals) is scripted, and the output says so.
"""
