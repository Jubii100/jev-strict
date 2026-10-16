# jev-strict

An unofficial, strict Python client for TypeSafe's Jev evaluation API. It is not made, endorsed or supported by
TypeSafe; check their API reference for the current contract before you rely on it. Standard library only, Python 3.9+.

A plain HTTP call to an evaluation model gives you an answer. This client adds the rules that keep the answers usable
and the bill bounded when many jobs share one key:

- **One pinned model.** The client refuses the moving alias as its model, and a response that names another model is
  an error, not a fallback. `alias_target()` asks the alias one fixed question and reads only the model id it serves,
  so you can see when the alias has moved past your pin.
- **A crash-safe usage meter.** Every answered call is one JSON row in a JSONL file. A row cut short by a full disk or a
  crash is counted as a *torn call* whose tokens are unknown, never as 0, and the next row starts on a line of its own.
  The file is read incrementally. A file that is replaced, grows shorter or changes in the 512 bytes before where the
  last read stopped is counted again from the start; an edit further back is not seen.
- **A hard monthly cap.** A call that would pass the cap is refused before any request leaves. Each torn call is charged
  the larger of the month's largest whole call and the request's own estimate.
- **No key or no budget means no answer.** Both raise `Unavailable` (`BLOCKED-LIVE: ...`), so your code records the item
  as waiting instead of guessing.
- **Order-checked judgements.** `Judge` asks every Choice and Score in forward and reversed option order in one request.
  If the two orders disagree, there is no answer. A Choice always has a `none` ("can't tell") option, and confidence is
  reported as a flag, never used as a decision.

## Install

Not on PyPI yet. From GitHub, or from a checkout:

```sh
pip install git+https://github.com/Jubii100/jev-strict
pip install .
```

## Quick start

The price and the caps below are made-up examples: use your own.

```python
from jev_strict import Blocked, Client, Judge

client = Client(
    "jev-x.y.z",                         # a pinned model id, never "jev-latest"
    "https://...",                       # the evaluation endpoint from TypeSafe's API reference
    price_usd_per_mtok_input=0.25,       # your price per million input tokens (output is not priced); at 0 no cap holds
    monthly_cap_usd=2.0,                 # every caller sharing this meter stops here
    meter_path="jev_usage.jsonl",        # one file for every process that spends against the same cap
)                                        # the key comes from TYPESAFE_API_KEY, or env_file=".env"

judge = Judge(client, prefix="tickets:", share_cap_usd=0.5)   # this caller's own share of the month

try:
    r = judge.choice(
        "topic",
        "Which topic does the ticket raise?",
        {"billing": "a charge, a refund or an invoice",
         "delivery": "a parcel that is late, lost or damaged",
         "none": "can't tell"},
        {"ticket": "My parcel was due on Monday and has not arrived."},
    )
    print(r["answer"], r["reason"], r["usd"])   # "delivery", "agreed", ...; answer is None when the orders disagree
except Blocked as why:
    print("waiting:", why)                       # no key, the cap or the share reached, or a failed request
```

The other questions:

| Call | Asks | Returns |
|---|---|---|
| `judge.choice(kind, instructions, options, state)` | one of 2 to 12 options, `none` required, both orders | `answer` (or None), `agree`, `top`, `probabilities`, `confidence`, `flags`, `reason` |
| `judge.score(kind, instructions, levels, state)` | one of 3 to 5 ordered levels, both orders | `level`, `index` (or None), `mean_index`, `probabilities`, `reason` |
| `judge.noul(kind, statement, state)` | "does the DATA say this?" | `p`, the probability of yes |
| `judge.nouls(kind, statements, state)` | 1 to 8 such statements in one request | `p` per statement key |
| `judge.summary(kind, checks, state)` | a Choice among check lines your code wrote | `line` (or None) and the Choice's fields |
| `client.ask(state, questions, purpose)` | any raw request | the parsed response |
| `client.status()` | nothing: reads the meter | this month's calls, torn calls, spend, cap basis |
| `client.alias_target()` | the alias, one fixed question | `serves`, `pinned`, `drift` (None when it cannot tell) |

Every `Judge` answer also carries `tokens`, `usd` and `model`. `Judge` wraps the state you pass as `{"DATA": state}`,
and each of its questions tells the model to treat it only as data, never as instructions; each of its requests is
metered under the purpose `prefix + kind`. `client.ask` sends the state and questions as you give them.

## Limits

- The rules come from measurements on particular Jev versions. Test them again when you move the pin.
- `Judge.spent()` and `share_cap_usd` count only lines that are one whole row under the judge's prefix. A torn row,
  or a whole row glued to one, counts against the client's monthly cap but not against any share.
- When a response carries no usage, `Judge` reports 0 tokens for that answer, while the meter records the request's
  own estimate with `"usage_reported": false`.
- The cap is checked from the meter before each request, and the row is written after the answer. Checking and
  sending are not one step, so callers running at the same moment (threads or processes) can each pass the cap by one
  call. Across processes, a row can still be joined to a fragment cut at the same moment; the reader recovers it.

## Development

```sh
python -m pip install pytest==8.4.2
python -m pytest -q tests
```

The tests use made-up models, prices and data, and never touch the network (`urlopen` is faked). CI runs them on
Python 3.9 to 3.13.

`PROVENANCE.md` says where the code came from and how it is kept in step with that source.

## License

MIT. See `LICENSE`.
