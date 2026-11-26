# Recordings

SYNTHETIC. Each folder is one recorded run (`aksha run ... --record FOLDER`): what the
person gave (`recording.json`: the agent, the message, the answers; `inputs/`: the
uploaded files), every model answer (`models.jsonl`), every search and fetch
(`search.jsonl`, `fetch.jsonl`), the outputs (`outputs.json`) and the run's trace as OTLP
JSON (`trace/`). `aksha replay FOLDER` runs the same agent again fully offline from it and
says whether the outputs are the same.

`research-occupancy/` was recorded from a fake run: the research agent's rule-based model
over the `stillroom` world's offline web (`AKSHA_SEARCH=world:stillroom`), with one
uploaded note. Every page and company in it is invented for testing. A live recording
(a real model and a real search API) has the same layout.
