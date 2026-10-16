# Provenance

jev-strict is a port of two modules of a larger private project, where they are the one Jev client for every
caller: a client with its usage meter and monthly cap, and a judge. The port keeps their logic and their tests; it
drops their configuration files, paths and command line.

**Import closure.** The standard library only. The package reads nothing outside itself: the meter path, the
optional env file, the endpoint, the model, the price and the cap are all arguments. The source modules read them
from the project's configuration and state.

## What changed in the port

- Configuration is passed in, not read from the project's files: `Client(model, endpoint, price_usd_per_mtok_input=…,
  monthly_cap_usd=…, meter_path=…)` and `Judge(client, prefix=…, share_cap_usd=…)`. The source's judge has a fixed
  prefix and reads its share from its configuration.
- The exceptions are `Unavailable` and `Error`. The cap message shows the cap as a float (a cap given as `2` reads
  "monthly cap of 2.0 USD …"); the source shows it as configured. The source's messages carry its own names where these
  say "monthly cap", "the share for", "model error", "a Choice", "a Score" and "a multi-Noul"; each message's facts
  are the same. The `User-Agent` header is an argument, `jev-strict/0.1` by default (the source sends its own
  project's).
- `Client` refuses an empty model or the moving alias as its pin with a `ValueError`. The source has no such check:
  its configuration names a fixed id.
- The per-caller share (`meter.share`, `Judge.spent`) skips a meter line that is not a JSON object, whose `ts` is
  not a string or whose count is not a number. The source's share stops with an exception on such a line. The share
  still counts only whole rows: a torn row has no readable purpose.
- The source's command line, report and setup checks are not ported. `status()` returns the same dictionary, and
  `torn_note()` the same words.

Everything else, including each rule, each threshold and each message's facts, matches the source.

## Tests

Each source test that covers ported code has a counterpart here, rewritten on made-up models, prices and content:

- **the meter and the cap** (14 source tests): every test in `test_meter.py` but the share test, and in
  `test_client.py` the two writer tests, the torn-call charge, `status()`, the unknown torn charge and the torn note;
- **the client** (3 source tests): `test_client.py`'s no-key and capped-month test, its other-model test and its
  alias-check test;
- **the judge** (6 source tests): the first six tests of `test_judge.py`, one for one.

The rest are here only: `test_client.py`'s missing answer, alias refused as the pin, env file, retry and refusal
tests; `test_meter.py`'s share test; `test_judge.py`'s judge through the real client. Source tests of the parts that
are not ported have no counterpart here.

## Keeping in step

In the source project, a parity test runs the source and this package on the same inputs and compares the results:
the meter's reading of each test corpus, `status()`, the cap decision and its boundary, each request as sent (URL,
method, headers but the `User-Agent`, body, timeout), the errors and retries, every `Judge` answer, and the share on
well-formed meters. A fix made to one side is made to the other in the same change, or the test fails.

A pull request here is reviewed before its code runs anywhere. A fix that lands here is ported back to the source
through the same parity test.
