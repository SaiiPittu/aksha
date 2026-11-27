# Patent agent: an example conversation

Continues in the same store after the research conversation. The agent asks for one detail the engineering note leaves open, recommends a next action for all four candidates, saves the person's selection, fills the forms and packet, and sends only after a separate approval of the exact action. The outputs are summarised in [outputs/patent-summary.md](outputs/patent-summary.md).

## Setup

```sh
make venv && . .venv/bin/activate
export AKSHA_HOME=var                 # this walk-through's state stays in ./var
export AKSHA_SEARCH=world:stillroom   # the world's offline web: vendor pages and publications
export AKSHA_SYSTEMS=counsel_mail=mailbox:var/mock-counsel   # the mock counsel mailbox
# AKSHA_MODE=fake is the default: rule-based fake models, no keys, no network
```

## 1. Ask the patent agent; it finds a detail the engineering note leaves open

```console
$ aksha run --agent patent 'Which of these inventions should we take to a patent attorney, and why? Prepare the selected disclosure documents.'
session S-<session>
mock: counsel_mail is a development mailbox at <work>/mock-counsel/mailbox.json (AKSHA_SYSTEMS); nothing it receives leaves this machine
R-0002 patent waiting
  waiting for input (missing detail) I-0002: aksha answer I-0002 TEXT
    asks: C2: the note leaves Minimum distance from a vent undecided. What is it?
(exit code 3)
```

## 2. Give the missing detail; it recommends a next action for all four candidates and asks which to prepare

```console
$ aksha answer I-0002 '0.5 m from the vent edge, so its air flow stays outside the footprint.'
mock: counsel_mail is a development mailbox at <work>/mock-counsel/mailbox.json (AKSHA_SYSTEMS); nothing it receives leaves this machine
R-0002 patent waiting
  waiting for choice (candidate selection) I-0003: aksha answer I-0003 TEXT
    asks: Which candidate should be prepared for counsel review? C1: prioritize_counsel_review; C2: collect_more_evidence; C3: deprioritize_for_now; C4: collect_more_evidence
    options: C1, C2, C3, C4
(exit code 3)
```

## 3. Choose C1; the selection is saved, the forms and packet are filled, and the send waits for its own approval

```console
$ aksha answer I-0003 C1
mock: counsel_mail is a development mailbox at <work>/mock-counsel/mailbox.json (AKSHA_SYSTEMS); nothing it receives leaves this machine
R-0002 patent waiting
  waiting for approval A-0001: aksha approve A-0001 --signature v1:58f18363f4f77cd2fcea3e1d8daeb6e84291e0782393a30ed22541444dcf4b28
    approves: send_packet
    packet: patent.packet/C1@v1
    recipient: counsel-primary
(exit code 4)
```

## 4. Approve the exact send shown; the mock mailbox receives the packet once

```console
$ aksha approve A-0001 --signature v1:58f18363f4f7
mock: counsel_mail is a development mailbox at <work>/mock-counsel/mailbox.json (AKSHA_SYSTEMS); nothing it receives leaves this machine
R-0002 patent ended complete
(exit code 0)
```

## 5. Approving again delivers nothing

```console
$ aksha approve A-0001 --signature v1:58f18363f4f7
mock: counsel_mail is a development mailbox at <work>/mock-counsel/mailbox.json (AKSHA_SYSTEMS); nothing it receives leaves this machine
refused: already_decided (command c-de4346edb69d633ddf8daee3, seq 1337)
(exit code 7)
```

## 6. The recommendation for C1, with its rule, risks and what would change it

```console
$ aksha show patent.recommendation/C1
{
  "content": {
    "based_on": [
      "patent.extraction/C1@v1",
      "patent.comparison/C1@v1"
    ],
    "candidate": "C1",
    "next_action": "prioritize_counsel_review",
    "risks": [
      "own public disclosure DISC-01 on 2026-03-10 (trade-show demo)",
      "outside contributor P-05 suggested F2, F3 on 2026-01-05, before the note"
    ],
    "rule": "R1",
    "why": "business priority 1; implementation: built_and_tested; F3 not found in any family searched; family SYN-PRI-2021-0412 discloses F1, F5; family SYN-PRI-2022-0118 discloses F2",
    "would_change_if": "a reference discloses F3, or an outside contributor's role changes inventorship or ownership"
  },
  "freshness": "current",
  "ref": "patent.recommendation/C1@v1"
}
(exit code 0)
```

## 7. Where every run stands, read from the event store

```console
$ aksha status
R-0001 research ended complete
R-0002 patent ended complete
(exit code 0)
```

## 8. Check the event store's hash chain

```console
$ aksha verify
chain ok: 1337 events, head sha256:c00353df66664066929172889405f0f71fe0142c121f6992ea664f3b70c7eea7
(exit code 0)
```
