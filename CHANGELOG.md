# Changelog

All notable changes to jev-strict. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the
project uses [Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-10-16

### Added

- `Client`: one pinned model (the moving alias is refused as a pin, and a response from another model is an error),
  the key from the environment or an env file and never shown, retries on busy or server-error replies (429, 500,
  502, 503, 504, 529) and on network errors and timeouts only, a missing answer as an error, and `alias_target()` to see which model the alias serves.
- The usage meter: one JSON row per answered call, read incrementally; torn rows are recovered or counted as calls of
  unknown size, never 0; a meter edited in place or replaced is counted again; the writer never joins a row to a
  fragment.
- The monthly cap, checked before any request leaves, with each torn call charged the larger of the month's largest
  call and the request's own estimate; `status()` and `torn_note()` show the cap basis.
- `Judge`: Choice (with a required `none` option) and Score asked in both option orders in one request, Noul and
  multi-Noul readings of the data, and a summary as a Choice among code-written check lines; a per-caller share of the
  month on top of the cap; `Blocked` when the model cannot be asked.
- CI: the tests on Python 3.9 to 3.13 for every push and pull request, every action pinned to a commit.
