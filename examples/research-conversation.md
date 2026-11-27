# Research agent: an example conversation

A real offline run on the synthetic stillroom world, through the command line. The request names the comparison dimensions and the review themes; the agent asks for the buyer segment and the geography because both change which competitors and reviews count. The outputs are in [outputs/](outputs/), summarised in [outputs/research-summary.md](outputs/research-summary.md).

## Setup

```sh
make venv && . .venv/bin/activate
export AKSHA_HOME=var                 # this walk-through's state stays in ./var
export AKSHA_SEARCH=world:stillroom   # the world's offline web: vendor pages and publications
export AKSHA_SYSTEMS=counsel_mail=mailbox:var/mock-counsel   # the mock counsel mailbox
# AKSHA_MODE=fake is the default: rule-based fake models, no keys, no network
```

## 1. Copy the synthetic stillroom inputs to a folder of your own

```console
$ aksha example stillroom inputs
copied 22 synthetic input files of stillroom to inputs
next: aksha run --agent research --input inputs "<your request>"
its vendor pages are on the world's offline web: set AKSHA_SEARCH=world:stillroom so web_search and fetch_url reach them
(exit code 0)
```

## 2. Ask the research agent; it asks only the questions that change the result

```console
$ aksha run --agent research --input inputs 'Who sells camera-free occupancy sensing, and at what price? Compare power (PoE, Ethernet, mains, USB-C, battery powered, battery thermal, AA), battery life (battery life, years of battery, months at) and privacy (privacy, camera, faces, identities, image). What do customers say about: battery (batter*, not battery powered); stillness (sit*, sat, seated, still, do not move, barely move); setup (cabl*, electrician, socket*, wiring, install*, setup); privacy (privacy, works council, image)?'
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
R-0001 research waiting
  waiting for input (question) I-0001: aksha answer I-0001 TEXT
    asks (--answer segment=TEXT): Which buyers is this for? It decides which competitors and reviews count.
    asks (--answer geography=TEXT): Which geography should this cover? Prices and reviews can come from several countries, so it changes what counts.
(exit code 3)
```

## 3. Answer both questions; the run finishes

```console
$ aksha answer I-0001 --answer segment=offices --answer geography=EU
mock: counsel_mail is a development mailbox at <work>/mock-counsel/mailbox.json (AKSHA_SYSTEMS); nothing it receives leaves this machine
R-0001 research ended complete
(exit code 0)
```

## 4. List what it saved, each output with its version

```console
$ aksha show
research.competitors@v1
research.findings@v1
research.matrix@v1
research.needs@v1
research.profile/c01-kelvra-systems@v1
research.profile/c02-noctra@v1
research.profile/c03-brightfield-analytics@v1
research.profile/c04-veyra-labs@v1
research.profile/c05-tallyway@v1
research.scope@v1
research.tests@v1
(exit code 0)
```

## 5. Print one output: the competitor matrix, every cell cited

```console
$ aksha show research.matrix
{
  "content": {
    "columns": [
      "company",
      "prices",
      "power",
      "battery_life",
      "privacy"
    ],
    "dimensions": [
      {
        "id": "power",
        "name": "power"
      },
      {
        "id": "battery_life",
        "name": "battery life"
      },
      {
        "id": "privacy",
        "name": "privacy"
      }
    ],
    "note": "each cell lists its entries with their page and date; prices are as listed, never converted or averaged",
    "pages_found": 6,
    "resolved": 5,
    "rows": [
      {
        "aliases": [
          "Kelvra TS-32"
        ],
        "cells": {
          "battery_life": [
            {
              "cite": {
                "artifact": "research.profile/c01-kelvra-systems@v1",
                "path": "/cells/battery_life/0"
              },
              "question": "What does Kelvra Systems state about battery life? Its pages do not say.",
              "reason": "not_found",
              "status": "unknown"
            }
          ],
          "power": [
            {
              "cite": {
                "quote": "A 32 x 32 thermal array for meeting rooms and open desks. Powered over Ethernet (PoE), ceiling mounted.",
                "span": "EX-0056"
              },
              "label": "fact",
              "passage": "text",
              "published": "2026-10-14",
              "record": "web/kelvra.example/product",
              "value": "A 32 x 32 thermal array for meeting rooms and open desks. Powered over Ethernet (PoE), ceiling mounted."
            },
            {
              "cite": {
                "quote": "Installation requires a PoE cable run to every sensor.",
                "span": "EX-0072"
              },
              "label": "fact",
              "passage": "text",
              "published": "2026-10-14",
              "record": "web/kelvra.example/product",
              "value": "Installation requires a PoE cable run to every sensor."
            }
          ],
          "privacy": [
            {
              "cite": {
                "quote": "Thermal only. The sensor cannot capture faces or identities.",
                "span": "EX-0071"
              },
              "label": "fact",
              "passage": "text",
              "published": "2026-10-14",
              "record": "web/kelvra.example/product",
              "value": "Thermal only. The sensor cannot capture faces or identities."
            }
          ]
        },
        "company": {
          "cite": {
            "quote": "Kelvra Systems",
            "span": "EX-0074"
          },
          "value": "Kelvra Systems"
        },
        "key": "c01-kelvra-systems",
        "price_conflict": false,
        "prices": [
          {
            "cite": {
              "quote": "EUR 249 per unit",
              "span": "EX-0064"
            },
            "currency": "EUR",
            "item": "Kelvra TS-32 sensor",
            "label": "fact",
            "number": "249",
            "passage": "price",
            "per": "unit",
            "published": "2026-10-14",
            "record": "web/kelvra.example/product",
            "terms": "one-off",
            "value": "249"
          },
          {
            "cite": {
              "quote": "EUR 6 per sensor per month",
              "span": "EX-0068"
            },
            "currency": "EUR",
            "item": "Workplace analytics",
            "label": "fact",
            "number": "6",
            "passage": "price",
            "per": "sensor per month",
            "published": "2026-10-14",
            "record": "web/kelvra.example/product",
            "terms": "billed annually",
            "value": "6"
          }
        ]
      },
      {
        "aliases": [
          "Noctra Air"
        ],
        "cells": {
          "battery_life": [
            {
              "cite": {
                "quote": "Battery powered, 5 year battery life claimed. CO2 accuracy +/- 50 ppm.",
                "span": "EX-0087"
              },
              "label": "vendor_claim",
              "passage": "claim",
              "published": "2026-07-08",
              "record": "web/noctra.example/product",
              "value": "Battery powered, 5 year battery life claimed. CO2 accuracy +/- 50 ppm."
            }
          ],
          "power": [
            {
              "cite": {
                "quote": "Battery powered, 5 year battery life claimed. CO2 accuracy +/- 50 ppm.",
                "span": "EX-0087"
              },
              "label": "vendor_claim",
              "passage": "claim",
              "published": "2026-07-08",
              "record": "web/noctra.example/product",
              "value": "Battery powered, 5 year battery life claimed. CO2 accuracy +/- 50 ppm."
            }
          ],
          "privacy": [
            {
              "cite": {
                "artifact": "research.profile/c02-noctra@v1",
                "path": "/cells/privacy/0"
              },
              "question": "What does Noctra state about privacy? Its pages do not say.",
              "reason": "not_found",
              "status": "unknown"
            }
          ]
        },
        "company": {
          "cite": {
            "quote": "Noctra",
            "span": "EX-0089"
          },
          "value": "Noctra"
        },
        "key": "c02-noctra",
        "price_conflict": false,
        "prices": [
          {
            "cite": {
              "artifact": "research.profile/c02-noctra@v1",
              "path": "/prices/0"
            },
            "question": "What does Noctra charge? No price is listed on its pages.",
            "reason": "not_found",
            "status": "unknown"
          }
        ]
      },
      {
        "aliases": [
          "OccuPoint",
          "Corvane Group",
          "Corvane Presence P1"
        ],
        "cells": {
          "battery_life": [
            {
              "cite": {
                "quote": "Battery life up to 3 years.",
                "span": "EX-0122"
              },
              "label": "vendor_claim",
              "passage": "claim",
              "published": "2026-04-02",
              "record": "web/brightfield.example/product",
              "value": "Battery life up to 3 years."
            },
            {
              "cite": {
                "quote": "Battery thermal and PIR sensor. Battery life up to 3 years.",
                "span": "EX-0147"
              },
              "label": "vendor_claim",
              "passage": "claim",
              "published": "2026-09-15",
              "record": "web/corvane.example/product",
              "value": "Battery thermal and PIR sensor. Battery life up to 3 years."
            }
          ],
          "power": [
            {
              "cite": {
                "quote": "A battery thermal and PIR desk and room sensor from Brightfield Analytics.",
                "span": "EX-0108"
              },
              "label": "fact",
              "passage": "text",
              "published": "2026-04-02",
              "record": "web/brightfield.example/product",
              "value": "A battery thermal and PIR desk and room sensor from Brightfield Analytics."
            },
            {
              "cite": {
                "quote": "Battery thermal and PIR sensor. Battery life up to 3 years.",
                "span": "EX-0147"
              },
              "label": "vendor_claim",
              "passage": "claim",
              "published": "2026-09-15",
              "record": "web/corvane.example/product",
              "value": "Battery thermal and PIR sensor. Battery life up to 3 years."
            }
          ],
          "privacy": [
            {
              "cite": {
                "artifact": "research.profile/c03-brightfield-analytics@v1",
                "path": "/cells/privacy/0"
              },
              "question": "What does Brightfield Analytics state about privacy? Its pages do not say.",
              "reason": "not_found",
              "status": "unknown"
            }
          ]
        },
        "company": {
          "cite": {
            "quote": "Brightfield Analytics",
            "span": "EX-0124"
          },
          "value": "Brightfield Analytics"
        },
        "key": "c03-brightfield-analytics",
        "price_conflict": true,
        "prices": [
          {
            "cite": {
              "quote": "USD 229 per unit",
              "span": "EX-0116"
            },
            "currency": "USD",
            "item": "OccuPoint sensor",
            "label": "fact",
            "number": "229",
            "passage": "price",
            "per": "unit",
            "published": "2026-04-02",
            "record": "web/brightfield.example/product",
            "terms": "one-off",
            "value": "229"
          },
          {
            "cite": {
              "quote": "USD 7 per sensor per month",
              "span": "EX-0120"
            },
            "currency": "USD",
            "item": "Occupancy insights",
            "label": "fact",
            "number": "7",
            "passage": "price",
            "per": "sensor per month",
            "published": "2026-04-02",
            "record": "web/brightfield.example/product",
            "terms": "billed annually",
            "value": "7"
          },
          {
            "cite": {
              "quote": "USD 199 per unit",
              "span": "EX-0141"
            },
            "currency": "USD",
            "item": "Corvane Presence P1 sensor",
            "label": "fact",
            "number": "199",
            "passage": "price",
            "per": "unit",
            "published": "2026-09-15",
            "record": "web/corvane.example/product",
            "terms": "one-off, launch offer",
            "value": "199"
          },
          {
            "cite": {
              "quote": "USD 7 per sensor per month",
              "span": "EX-0145"
            },
            "currency": "USD",
            "item": "Occupancy insights",
            "label": "fact",
            "number": "7",
            "passage": "price",
            "per": "sensor per month",
            "published": "2026-09-15",
            "record": "web/corvane.example/product",
            "terms": "billed annually",
            "value": "7"
          }
        ]
      },
      {
        "aliases": [
          "Veyra Halo"
        ],
        "cells": {
          "battery_life": [
            {
              "cite": {
                "artifact": "research.profile/c04-veyra-labs@v1",
                "path": "/cells/battery_life/0"
              },
              "question": "What does Veyra Labs state about battery life? Its pages do not say.",
              "reason": "not_found",
              "status": "unknown"
            }
          ],
          "power": [
            {
              "cite": {
                "quote": "Mains powered (USB-C, 5 V). Detection range up to 6 m.",
                "span": "EX-0175"
              },
              "label": "vendor_claim",
              "passage": "claim",
              "published": "2026-09-30",
              "record": "web/veyra.example/product",
              "value": "Mains powered (USB-C, 5 V). Detection range up to 6 m."
            }
          ],
          "privacy": [
            {
              "cite": {
                "artifact": "research.profile/c04-veyra-labs@v1",
                "path": "/cells/privacy/0"
              },
              "question": "What does Veyra Labs state about privacy? Its pages do not say.",
              "reason": "not_found",
              "status": "unknown"
            }
          ]
        },
        "company": {
          "cite": {
            "quote": "Veyra Labs",
            "span": "EX-0177"
          },
          "value": "Veyra Labs"
        },
        "key": "c04-veyra-labs",
        "price_conflict": false,
        "prices": [
          {
            "cite": {
              "quote": "EUR 319 per unit",
              "span": "EX-0169"
            },
            "currency": "EUR",
            "item": "Veyra Halo sensor",
            "label": "fact",
            "number": "319",
            "passage": "price",
            "per": "unit",
            "published": "2026-09-30",
            "record": "web/veyra.example/product",
            "terms": "one-off",
            "value": "319"
          },
          {
            "cite": {
              "quote": "EUR 9 per sensor per month",
              "span": "EX-0173"
            },
            "currency": "EUR",
            "item": "Presence API",
            "label": "fact",
            "number": "9",
            "passage": "price",
            "per": "sensor per month",
            "published": "2026-09-30",
            "record": "web/veyra.example/product",
            "terms": "billed monthly",
            "value": "9"
          }
        ]
      },
      {
        "aliases": [
          "Tallyway Beam"
        ],
        "cells": {
          "battery_life": [
            {
              "cite": {
                "quote": "Up to 7 years of battery life. Install it and forget it.",
                "span": "EX-0188"
              },
              "label": "vendor_claim",
              "passage": "claim",
              "published": "2026-08-21",
              "record": "web/tallyway.example/product",
              "value": "Up to 7 years of battery life. Install it and forget it."
            },
            {
              "cite": {
                "quote": "7 years at 1 report per 10 minutes; 14 months at 1 report per minute (typical office setting)",
                "span": "EX-0208"
              },
              "label": "fact",
              "passage": "spec",
              "published": "2026-08-21",
              "record": "web/tallyway.example/product",
              "value": "7 years at 1 report per 10 minutes; 14 months at 1 report per minute (typical office setting)"
            }
          ],
          "power": [
            {
              "cite": {
                "quote": "A time-of-flight (ToF) doorway counter. Counts people in and out of a room; battery powered.",
                "span": "EX-0187"
              },
              "label": "fact",
              "passage": "text",
              "published": "2026-08-21",
              "record": "web/tallyway.example/product",
              "value": "A time-of-flight (ToF) doorway counter. Counts people in and out of a room; battery powered."
            },
            {
              "cite": {
                "quote": "2 x AA lithium",
                "span": "EX-0211"
              },
              "label": "fact",
              "passage": "spec",
              "published": "2026-08-21",
              "record": "web/tallyway.example/product",
              "value": "2 x AA lithium"
            }
          ],
          "privacy": [
            {
              "cite": {
                "artifact": "research.profile/c05-tallyway@v1",
                "path": "/cells/privacy/0"
              },
              "question": "What does Tallyway state about privacy? Its pages do not say.",
              "reason": "not_found",
              "status": "unknown"
            }
          ]
        },
        "company": {
          "cite": {
            "quote": "Tallyway",
            "span": "EX-0213"
          },
          "value": "Tallyway"
        },
        "key": "c05-tallyway",
        "price_conflict": false,
        "prices": [
          {
            "cite": {
              "quote": "USD 189 per unit",
              "span": "EX-0196"
            },
            "currency": "USD",
            "item": "Tallyway Beam counter",
            "label": "fact",
            "number": "189",
            "passage": "price",
            "per": "unit",
            "published": "2026-08-21",
            "record": "web/tallyway.example/product",
            "terms": "one-off",
            "value": "189"
          },
          {
            "cite": {
              "quote": "USD 5 per counter per month",
              "span": "EX-0200"
            },
            "currency": "USD",
            "item": "Cloud dashboard",
            "label": "fact",
            "number": "5",
            "passage": "price",
            "per": "counter per month",
            "published": "2026-08-21",
            "record": "web/tallyway.example/product",
            "terms": "billed annually",
            "value": "5"
          }
        ]
      }
    ]
  },
  "freshness": "current",
  "ref": "research.matrix@v1"
}
(exit code 0)
```
