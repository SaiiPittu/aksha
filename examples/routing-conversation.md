# Routing: a request with no agent named

Without `--agent`, the router matches the request against each loaded agent's declared purpose. An unclear request gets a route question; a clear one goes straight to its agent, and every run line names the agent.

## Setup

```sh
make venv && . .venv/bin/activate
export AKSHA_HOME=var                 # this walk-through's state stays in ./var
export AKSHA_SEARCH=world:stillroom   # the world's offline web: vendor pages and publications
export AKSHA_SYSTEMS=counsel_mail=mailbox:var/mock-counsel   # the mock counsel mailbox
# AKSHA_MODE=fake is the default: rule-based fake models, no keys, no network
```

## 1. Copy the inputs

```console
$ aksha example stillroom inputs
copied 22 synthetic input files of stillroom to inputs
next: aksha run --agent research --input inputs "<your request>"
its vendor pages are on the world's offline web: set AKSHA_SEARCH=world:stillroom so web_search and fetch_url reach them
(exit code 0)
```

## 2. An unclear request with no agent named: the router asks which agent is meant

```console
$ aksha run --input inputs 'Help me with our sensor.'
mock: counsel_mail is a development mailbox at <work>/mock-counsel/mailbox.json (AKSHA_SYSTEMS); nothing it receives leaves this machine
uploaded 22 input files
  contributors/records.csv: D-0001 v1, text/csv, as given, 1478 characters
  disclosures/2026-03-10-trade-show-demo.md: D-0002 v1, text/markdown, as given, 995 characters
  disclosures/consultant-email.eml.txt: D-0003 v1, message/rfc822, as given, 691 characters
  disclosures/history.csv: D-0004 v1, text/csv, as given, 1088 characters
  notes/C1-self-calibration.md: D-0005 v1, text/markdown, as given, 2162 characters
  notes/C2-sensor-placement.md: D-0006 v1, text/markdown, as given, 1513 characters
  notes/C3-privacy-preserving-coordination.md: D-0007 v1, text/markdown, as given, 1643 characters
  notes/C4-battery-saving-sampling.md: D-0008 v1, text/markdown, as given, 1486 characters
  priorities/business-priorities.md: D-0009 v1, text/markdown, as given, 1081 characters
  publications/SYN-PUB-0001.json: D-0010 v1, application/json, as given, 1745 characters
  publications/SYN-PUB-0002.json: D-0011 v1, application/json, as given, 1730 characters
  publications/SYN-PUB-0003.json: D-0012 v1, application/json, as given, 1553 characters
  publications/SYN-PUB-0004.json: D-0013 v1, application/json, as given, 1580 characters
  publications/SYN-PUB-0005.json: D-0014 v1, application/json, as given, 1847 characters
  publications/SYN-PUB-0006.pdf.txt: D-0015 v1, text/plain, as given, 1427 characters
  publications/SYN-PUB-0007.json: D-0016 v1, application/json, as given, 1569 characters
  publications/SYN-PUB-0008.json: D-0017 v1, application/json, as given, 1721 characters
  publications/SYN-PUB-0009.json: D-0018 v1, application/json, as given, 1435 characters
  publications/SYN-PUB-0010.json: D-0019 v1, application/json, as given, 1636 characters
  reviews/reviews.json: D-0020 v1, application/json, as given, 14597 characters
  templates/filing-information-worksheet.md: D-0021 v1, text/markdown, as given, 1276 characters
  templates/invention-disclosure-form.md: D-0022 v1, text/markdown, as given, 1618 characters
session S-<session>
mock: counsel_mail is a development mailbox at <work>/mock-counsel/mailbox.json (AKSHA_SYSTEMS); nothing it receives leaves this machine
session S-<session> waiting for route I-0001 (aksha answer I-0001 --agent NAME)
    asks: Prepare any venture's invention candidates for counsel review from its own papers: extract each mechanism, its contributors and dated disclosures, search and compare prior art, recommend a next step, and send a counsel-review packet only after a person approves it, or research who sells a product, at what price and what customers say, over the person's own inputs: competitors, prices, the dimensions the request names, reviews and needs, every claim cited to a record read?
    options: patent, research
(exit code 3)
```

## 3. Name the agent; the run starts on the patent agent and holds on the default budget

Note: this routed run recorded no budget of its own on `run.started`, so it runs on the runtime default (`DEFAULT_BUDGET`, 40 tool calls) and holds on a budget interrupt the same request run with `--agent` does not reach.

```console
$ aksha answer I-0001 --agent patent
mock: counsel_mail is a development mailbox at <work>/mock-counsel/mailbox.json (AKSHA_SYSTEMS); nothing it receives leaves this machine
R-0001 patent waiting
  waiting for budget budget-1: aksha answer budget-1 TEXT
(exit code 3)
```

## 4. A clear request with no --agent: the router picks the research agent from the agents' declared purposes, with no question

Note: this routed run recorded no budget of its own on `run.started`, so it runs on the runtime default (`DEFAULT_BUDGET`, 40 tool calls) and holds on a budget interrupt the same request run with `--agent` does not reach.

```console
$ aksha run 'Who sells occupancy sensors for offices in the EU, and at what price? What do customers say about: battery (batter*); setup (install*, cabl*, electrician)?'
session S-<session>
mock: counsel_mail is a development mailbox at <work>/mock-counsel/mailbox.json (AKSHA_SYSTEMS); nothing it receives leaves this machine
R-0002 research waiting
  waiting for budget budget-1: aksha answer budget-1 TEXT
(exit code 3)
```

## 5. Each run line names the agent that handled it

```console
$ aksha status
R-0001 patent running
R-0002 research waiting
  waiting for budget budget-1: aksha answer budget-1 TEXT
(exit code 0)
```
