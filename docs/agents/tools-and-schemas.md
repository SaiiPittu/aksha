# Tools and schemas

This page lists every tool the research and patent agents can call: what it does, its effect class, which Tasks may call it, its limits, what happens to it after a crash, and its JSON input schema. The schemas in the last section are generated from each tool's `ToolSpec`, the same frozen copy the model is shown and the policy gate validates against ([`src/aksha/runtime/tools/registry.py`](../../src/aksha/runtime/tools/registry.py)). They are not typed by hand.

## How a tool is declared

A tool is a `ToolSpec` ([`src/aksha/runtime/tools/types.py`](../../src/aksha/runtime/tools/types.py)) paired with a body in a `ToolDecl`. The spec carries the name, the owner, one effect class, the description, the JSON Schema of its arguments, its `RetryPolicy`, and, for an external tool, its `EffectSpec` and `ApprovalRule`. A body receives a `ToolContext` and validated arguments and returns a `ToolOutcome`; it has no handle on the event store, so it cannot write history itself.

Three tools are added by the loader to every agent and cannot be declared by one: `finish`, `ask` and `save_artifact` ([`src/aksha/runtime/tools/loader_added.py`](../../src/aksha/runtime/tools/loader_added.py)). `finish` is offered on every Task, `ask` only on a Task with a declared interrupt, and `save_artifact` only where a Task lists it.

## Effect classes and what happens after a crash

| Effect class | Meaning | Step semantics | After a crash |
|---|---|---|---|
| `read` | Reads inputs or the web; changes nothing | `at_least_once` | A completed step's recorded result is reused; an interrupted one runs again. |
| `store` | Asks the artifact store to save a version | `at_least_once` | The same: the save is written with the step's completion. |
| `external` | An effect outside the system, through the transactional outbox | `reconcile` | The outbox asks the receiver, by the tool's key fields, whether the effect landed, and sends only if it did not. |
| `reserved` | `finish` and `ask`, handled by the runtime; no body | runtime | The stop decision or the open interrupt is a recorded fact. |

The semantics are the defaults of ADR-P02 D4 ([`ADR-P02`](../design/adr/ADR-P02-durable-execution.md)). Tests: [`tests/integration/test_durable_execution.py`](../../tests/integration/test_durable_execution.py), [`tests/integration/test_outbox.py`](../../tests/integration/test_outbox.py), [`tests/integration/test_sigkill_recovery.py`](../../tests/integration/test_sigkill_recovery.py).

A recorded query (`recorded_query=True`) is versioned by a hash of what it returned, so an input added later makes it run again on a rebuild, and a rebuild that returns the same hits stops there.

## Every tool

| Tool | Owner | Effect | Research Tasks | Patent Tasks | Limits | Idempotency |
|---|---|---|---|---|---|---|
| `list_inputs` | the agent (shared harness tool) | `read`, recorded query | `scope`, `discover`, `voice` | `scope`, `extract`, `prepare` | `limit` at most 500 entries; read retry policy | Re-runnable; versioned by its listing |
| `read_document` | the agent (shared) | `read`, mints spans | `scope`, `discover`, `evidence`, `voice` | `scope`, `extract`, `prepare`, `deep`, `prior_art` | `limit` at most 2,000 lines; window 16,000 characters (research) or 40,000 (patent) | Re-runnable; the read records the input's version |
| `search_inputs` | the agent (shared) | `read`, recorded query | `scope`, `discover`, `voice` | `scope`, `extract`, `prepare`, `deep`, `prior_art` | Hits are pointers, never citable; `kinds` searches only the inputs of those kinds | Re-runnable; versioned by the inputs it hit |
| `todo` | the agent (shared) | `read` | `scope`, `discover`, `evidence`, `voice` | `scope`, `extract` | One item in progress at a time | Re-runnable; the whole list is sent each call |
| `web_search` | the agent (shared) | `read`, egress `aksha.web` | `discover` | `deep`, `prior_art` | `query` 2 to 400 characters, `limit` 1 to 20; 60,000 ms timeout, two retries | Re-runnable; mints nothing, a snippet is never evidence |
| `fetch_url` | the agent (shared) | `read`, egress `aksha.web`, mints spans | `discover`, `evidence` | `deep`, `prior_art` | `url` 8 to 2,000 characters; read retry policy | Re-runnable; the page is read as record `web/<host><path>` at its version |
| `finish` | runtime | `reserved` | every Task | every Task | The completion check decides; `max_finish_vetoes` refusals per entry | A proposal, never an effect |
| `ask` | runtime | `reserved` | `scope` | `extract`, `select` | Only where the Task declares an interrupt | One open interrupt; the answer is a recorded fact |
| `save_artifact` | runtime | `store` | every Task | every Task but `candidate` and `prepare` | Only the kinds in the Task's `writes`; content checked against the kind's schema and cite policies | A new version per save |
| `commit_round` | patent (portfolio extension) | `store` | none | `candidate` | Deep tier cap 10, deep budget 100 turns, batch cap 10; the whole round is refused when one is broken | A new round per commit |
| `read_form` | the agent (shared harness tool) | `read`, mints spans | none | `scope`, `prepare` | Read retry policy; refuses an input that is not a blank form (`not_a_form`) or holds no field (`no_fields`) | Re-runnable; the read records the form's version |
| `fill_form` | the agent (shared harness tool) | `store`, writes the agent's form kind (`patent.form`) | none | `prepare` | Refuses a field list that is not the form's (`fields_mismatch`), an unknown status (`status_invalid`), a `complete` field with no value (`complete_needs_value`), and a `complete` field only the confirmer completes or with an open question (`confirm_only`) | One form version per key and form (`<candidate>~<template>`) |
| `send_packet` | patent | `external`, system `counsel_mail` | none | `send` | Needs an approval of the exact packet version and recipient; the recipient matches `^[A-Za-z0-9][A-Za-z0-9._@+-]{0,127}$` | `reconcile` on `("/packet", "/recipient")`: the same packet to the same recipient is sent at most once |

The read retry policy is a 600,000 ms timeout with one retry for both agents (`READING.retry`); other tools use the default of 10,000 ms with two retries. The `deep` row lists the tools of the `deep` parent, which its child `prior_art` uses.

Two tools keep the model from writing what code must decide:

1. `fill_form`, the harness's form tool ([`src/aksha/runtime/forms/tools.py`](../../src/aksha/runtime/forms/tools.py)), checks the fields against the blank form in `form_refusal` and records `required` from the form, never from the model. The agent declares its forms as data (`FormSpec`: the patent agent's is `tools.FORMS`, with counsel as the confirmer and `needs_counsel_confirmation` as the confirm status); the rules are the runtime's. Tested with a checklist agent in [`tests/unit/runtime/forms/test_tools.py`](../../tests/unit/runtime/forms/test_tools.py) and for the patent agent in [`tests/agents/test_patent_units.py`](../../tests/agents/test_patent_units.py) (`test_fill_form_refuses_marking_a_counsel_field_complete`, `test_fill_form_records_required_from_the_template_not_the_model`).
2. `send_packet`'s body never sends: it returns `outbox_only`, and only the outbox delivers, after the approval (`test_send_needs_an_approval_and_goes_through_the_outbox`).

`save_artifact`'s published schema is generic. What the model is shown is made per Task: the description names each kind the Task writes, whether it takes a key, which fields must cite, and the kind's content schema (`offered_for_task`). The kinds are listed in [research.md](research.md#outputs) and [patent.md](patent.md#outputs).

## Input schemas, generated

Regenerate this section from the definitions with:

```python
import json
from aksha.agents.research.agent import AGENT as R
from aksha.agents.patent.agent import AGENT as P
from aksha.runtime.tools.loader_added import LOADER_ADDED_DECLS
from aksha.runtime.tools.types import ToolDecl
from aksha.runtime.canonical import thaw

for agent in (R, P):
    specs = {e.spec.name: e.spec for e in agent.tools if isinstance(e, ToolDecl)}
    specs.update({n: d.spec for n, d in LOADER_ADDED_DECLS.items()})
    for name, spec in sorted(specs.items()):
        print(agent.name, name, json.dumps(thaw(spec.schema), indent=2))
```

A schema shared by both agents is printed once. `list_inputs` differs only in its `kinds` enum, which comes from each agent's document kinds; `search_inputs` takes `kinds` only for the patent agent.

### `list_inputs`

For the research agent:

```json
{
  "type": "object",
  "properties": {
    "formats": {
      "type": "array",
      "items": {
        "enum": [
          "html",
          "markdown",
          "csv",
          "json",
          "email",
          "text"
        ]
      }
    },
    "offset": {
      "type": "integer",
      "minimum": 0
    },
    "limit": {
      "type": "integer",
      "minimum": 1,
      "maximum": 500
    },
    "kinds": {
      "type": "array",
      "items": {
        "enum": [
          "competitor_page",
          "pricing_table",
          "customer_reviews",
          "market_report",
          "conversation",
          "internal_note",
          "other"
        ]
      }
    }
  },
  "additionalProperties": false
}
```

For the patent agent:

```json
{
  "type": "object",
  "properties": {
    "formats": {
      "type": "array",
      "items": {
        "enum": [
          "html",
          "markdown",
          "csv",
          "json",
          "email",
          "text"
        ]
      }
    },
    "offset": {
      "type": "integer",
      "minimum": 0
    },
    "limit": {
      "type": "integer",
      "minimum": 1,
      "maximum": 500
    },
    "kinds": {
      "type": "array",
      "items": {
        "enum": [
          "invention_disclosure",
          "meeting_notes",
          "contributor_record",
          "disclosure_record",
          "email",
          "business_priorities",
          "form_template",
          "prior_art_publication",
          "other"
        ]
      }
    }
  },
  "additionalProperties": false
}
```

### `read_document`

```json
{
  "type": "object",
  "properties": {
    "ref": {
      "type": "string",
      "minLength": 1,
      "maxLength": 512
    },
    "offset": {
      "type": "integer",
      "minimum": 1
    },
    "limit": {
      "type": "integer",
      "minimum": 1,
      "maximum": 2000
    },
    "section": {
      "type": "integer",
      "minimum": 0
    },
    "cells": {
      "type": "boolean"
    },
    "where": {
      "type": "object",
      "properties": {
        "column": {
          "type": "string",
          "minLength": 1,
          "maxLength": 200
        },
        "values": {
          "type": "array",
          "minItems": 1,
          "maxItems": 50,
          "items": {
            "type": "string",
            "maxLength": 200
          }
        }
      },
      "required": [
        "column",
        "values"
      ],
      "additionalProperties": false
    },
    "extract": {
      "type": "boolean"
    }
  },
  "required": [
    "ref"
  ],
  "additionalProperties": false
}
```

### `search_inputs`

For the research agent:

```json
{
  "type": "object",
  "properties": {
    "query": {
      "type": "string",
      "minLength": 2,
      "maxLength": 400
    },
    "terms": {
      "type": "array",
      "minItems": 1,
      "maxItems": 40,
      "items": {
        "type": "string",
        "minLength": 2,
        "maxLength": 200
      }
    },
    "ref": {
      "type": "string",
      "minLength": 1,
      "maxLength": 512
    },
    "formats": {
      "type": "array",
      "items": {
        "enum": [
          "html",
          "markdown",
          "csv",
          "json",
          "email",
          "text"
        ]
      }
    },
    "match": {
      "enum": [
        "any",
        "all",
        "phrase"
      ]
    },
    "limit": {
      "type": "integer",
      "minimum": 1,
      "maximum": 100
    },
    "kinds": {
      "type": "array",
      "items": {
        "enum": [
          "competitor_page",
          "pricing_table",
          "customer_reviews",
          "market_report",
          "conversation",
          "internal_note",
          "other"
        ]
      }
    }
  },
  "anyOf": [
    {
      "required": [
        "query"
      ]
    },
    {
      "required": [
        "terms"
      ]
    }
  ],
  "additionalProperties": false
}
```

For the patent agent, which sets `search_kinds`, the same with a `kinds` property: an array whose items are its document kinds, as for `list_inputs`. The research agent's search takes no `kinds`.

### `todo`

```json
{
  "type": "object",
  "properties": {
    "todos": {
      "type": "array",
      "maxItems": 50,
      "items": {
        "type": "object",
        "properties": {
          "content": {
            "type": "string",
            "minLength": 1,
            "maxLength": 300
          },
          "status": {
            "enum": [
              "pending",
              "in_progress",
              "done"
            ]
          }
        },
        "required": [
          "content",
          "status"
        ],
        "additionalProperties": false
      }
    }
  },
  "required": [
    "todos"
  ],
  "additionalProperties": false
}
```

### `web_search`

```json
{
  "type": "object",
  "properties": {
    "query": {
      "type": "string",
      "minLength": 2,
      "maxLength": 400
    },
    "limit": {
      "type": "integer",
      "minimum": 1,
      "maximum": 20
    }
  },
  "required": [
    "query"
  ],
  "additionalProperties": false
}
```

### `fetch_url`

```json
{
  "type": "object",
  "properties": {
    "url": {
      "type": "string",
      "minLength": 8,
      "maxLength": 2000
    },
    "offset": {
      "type": "integer",
      "minimum": 1
    },
    "limit": {
      "type": "integer",
      "minimum": 1,
      "maximum": 2000
    },
    "cells": {
      "type": "boolean"
    },
    "extract": {
      "type": "boolean"
    }
  },
  "required": [
    "url"
  ],
  "additionalProperties": false
}
```

### `finish`

```json
{
  "type": "object",
  "properties": {
    "summary": {
      "type": "string"
    }
  },
  "additionalProperties": false
}
```

### `ask`

```json
{
  "type": "object",
  "properties": {
    "question": {
      "type": "string",
      "minLength": 1
    },
    "questions": {
      "type": "array",
      "minItems": 1,
      "items": {
        "type": "object",
        "properties": {
          "id": {
            "type": "string",
            "minLength": 1
          },
          "text": {
            "type": "string",
            "minLength": 1
          }
        },
        "required": [
          "id",
          "text"
        ],
        "additionalProperties": false
      }
    },
    "options": {
      "type": "array",
      "items": {
        "type": "string"
      }
    }
  },
  "anyOf": [
    {
      "required": [
        "question"
      ]
    },
    {
      "required": [
        "questions"
      ]
    }
  ],
  "additionalProperties": false
}
```

### `save_artifact`

```json
{
  "type": "object",
  "properties": {
    "kind": {
      "type": "string",
      "minLength": 1
    },
    "key": {
      "type": [
        "string",
        "null"
      ]
    },
    "content": {
      "type": "object"
    },
    "primary_inputs": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "kind": {
            "enum": [
              "record",
              "query",
              "statement",
              "artifact",
              "blob",
              "time"
            ]
          },
          "id": {
            "type": "string",
            "minLength": 1
          },
          "version": {
            "type": "string",
            "minLength": 1
          },
          "path": {
            "type": "string"
          },
          "entry_hash": {
            "type": "string",
            "pattern": "^sha256:[0-9a-f]{64}$"
          }
        },
        "required": [
          "kind",
          "id",
          "version"
        ],
        "additionalProperties": false
      }
    },
    "relations": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "kind": {
            "type": "string",
            "minLength": 1
          },
          "src": {
            "anyOf": [
              {
                "const": "self"
              },
              {
                "type": "object",
                "properties": {
                  "kind": {
                    "enum": [
                      "record",
                      "query",
                      "statement",
                      "artifact",
                      "blob",
                      "time"
                    ]
                  },
                  "id": {
                    "type": "string",
                    "minLength": 1
                  },
                  "version": {
                    "type": "string",
                    "minLength": 1
                  },
                  "path": {
                    "type": "string"
                  },
                  "entry_hash": {
                    "type": "string",
                    "pattern": "^sha256:[0-9a-f]{64}$"
                  }
                },
                "required": [
                  "kind",
                  "id",
                  "version"
                ],
                "additionalProperties": false
              }
            ]
          },
          "dst": {
            "anyOf": [
              {
                "const": "self"
              },
              {
                "type": "object",
                "properties": {
                  "kind": {
                    "enum": [
                      "record",
                      "query",
                      "statement",
                      "artifact",
                      "blob",
                      "time"
                    ]
                  },
                  "id": {
                    "type": "string",
                    "minLength": 1
                  },
                  "version": {
                    "type": "string",
                    "minLength": 1
                  },
                  "path": {
                    "type": "string"
                  },
                  "entry_hash": {
                    "type": "string",
                    "pattern": "^sha256:[0-9a-f]{64}$"
                  }
                },
                "required": [
                  "kind",
                  "id",
                  "version"
                ],
                "additionalProperties": false
              }
            ]
          },
          "label": {
            "type": [
              "string",
              "null"
            ]
          },
          "evidence": {
            "type": "array",
            "items": {
              "type": "object",
              "properties": {
                "source": {
                  "type": "object",
                  "properties": {
                    "kind": {
                      "enum": [
                        "record",
                        "query",
                        "statement",
                        "artifact",
                        "blob",
                        "time"
                      ]
                    },
                    "id": {
                      "type": "string",
                      "minLength": 1
                    },
                    "version": {
                      "type": "string",
                      "minLength": 1
                    },
                    "path": {
                      "type": "string"
                    },
                    "entry_hash": {
                      "type": "string",
                      "pattern": "^sha256:[0-9a-f]{64}$"
                    }
                  },
                  "required": [
                    "kind",
                    "id",
                    "version"
                  ],
                  "additionalProperties": false
                },
                "quote": {
                  "type": "string",
                  "minLength": 1
                },
                "pointer": {
                  "type": "string"
                },
                "span": {
                  "type": "string",
                  "minLength": 1
                }
              },
              "required": [
                "source",
                "quote"
              ],
              "additionalProperties": false
            }
          }
        },
        "required": [
          "kind",
          "src",
          "dst",
          "evidence"
        ],
        "additionalProperties": false
      }
    }
  },
  "required": [
    "kind",
    "content"
  ],
  "additionalProperties": false
}
```

### `commit_round`

```json
{
  "type": "object",
  "required": [
    "notes",
    "directives"
  ],
  "properties": {
    "notes": {
      "type": "object",
      "required": [
        "questions",
        "entries",
        "hypotheses"
      ],
      "properties": {
        "questions": {
          "type": "array",
          "items": {
            "type": "string"
          },
          "maxItems": 20
        },
        "entries": {
          "type": "array",
          "maxItems": 30,
          "items": {
            "type": "object",
            "required": [
              "keys",
              "text"
            ],
            "properties": {
              "keys": {
                "type": "array",
                "items": {
                  "type": "string"
                }
              },
              "text": {
                "type": "string",
                "maxLength": 300
              }
            }
          }
        },
        "hypotheses": {
          "type": "string",
          "maxLength": 1500
        }
      }
    },
    "plan": {
      "type": "object",
      "required": [
        "goal",
        "tier_rule",
        "budget_split"
      ],
      "properties": {
        "goal": {
          "type": "string",
          "maxLength": 2000
        },
        "tier_rule": {
          "type": "string",
          "maxLength": 2000
        },
        "budget_split": {
          "type": "string",
          "maxLength": 2000
        }
      }
    },
    "directives": {
      "type": "array",
      "items": {
        "type": "object",
        "required": [
          "keys",
          "state",
          "reason"
        ],
        "properties": {
          "keys": {
            "type": "array",
            "items": {
              "type": "string"
            },
            "minItems": 1
          },
          "state": {
            "enum": [
              "admitted",
              "dropped",
              "deferred"
            ]
          },
          "tier": {
            "type": [
              "string",
              "null"
            ]
          },
          "priority": {
            "type": "integer"
          },
          "reason": {
            "type": "string",
            "maxLength": 120
          }
        },
        "additionalProperties": false
      }
    }
  },
  "additionalProperties": false
}
```

### `read_form`

```json
{
  "type": "object",
  "properties": {
    "template": {
      "type": "string",
      "minLength": 1,
      "maxLength": 512
    }
  },
  "required": [
    "template"
  ],
  "additionalProperties": false
}
```

### `fill_form`

```json
{
  "type": "object",
  "properties": {
    "template": {
      "type": "string",
      "minLength": 1,
      "maxLength": 512
    },
    "key": {
      "type": "string",
      "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]*$"
    },
    "fields": {
      "type": "array",
      "items": {
        "type": "object"
      }
    }
  },
  "required": [
    "template",
    "key",
    "fields"
  ],
  "additionalProperties": false
}
```

### `send_packet`

```json
{
  "type": "object",
  "properties": {
    "packet": {
      "type": "string",
      "minLength": 1
    },
    "recipient": {
      "type": "string",
      "pattern": "^[A-Za-z0-9][A-Za-z0-9._@+-]{0,127}$"
    }
  },
  "required": [
    "packet",
    "recipient"
  ],
  "additionalProperties": false
}
```

