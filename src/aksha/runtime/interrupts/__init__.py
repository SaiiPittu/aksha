"""Interrupts and approvals (ADR-P17): interrupt, suspension and approval.

A run suspends on a durable interrupt and resumes on a person's recorded answer; an
approval authorises one exact action, bound to a signature over a generic action body.
Built: ``types.py`` (kinds, requests, views, ``ActionBody``), ``reducers.py`` (suspension
as a projection), ``raising.py`` (the ``interrupt.raised`` fact and the ``ask`` request),
``approvals.py`` (signature, body, states, the execution re-check and the outbox
authority), ``commands.py`` (the person's deciding commands and ``handoff``) and
``amend.py`` (an amended answer, ADR-P27 D5). Planned: the deferred call executed on
grant (P17-4), approval expiry (S7), the receipt artifact (S4).
"""
