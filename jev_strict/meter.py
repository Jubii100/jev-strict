"""The usage meter: one JSON row per billed call in a JSONL file, read incrementally, safe against torn writes.

A row is written only after the API answered, so every row is a billed call. A write cut short (a full disk, a crash)
leaves a fragment with no newline; the writer starts its next row on a line of its own, and the reader recovers the
whole rows of a line that holds one and counts each fragment as a *torn call*: billed, tokens unknown. Unknown is
never 0: the cap check charges each torn call the larger of the month's largest whole call and the request's own
estimate."""
from __future__ import annotations

import json
import math
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

_CACHE: dict = {}          # (path, month) -> [offset, calls, input_tokens, torn, max_call_tokens, file id, tail]: the
LOCK = threading.Lock()    # meter is read incrementally, so a batch of thousands of calls does not re-parse it per call
_TAIL = 512                # bytes before the read offset kept to notice an edit in place: more than one whole row

ROW_START = '{"ts"'        # every row the writer makes starts so (json.dumps, `ts` first)
_MONTH = r"\d{4}-(?:0[1-9]|1[0-2])"                         # YYYY-MM, a real month: what a call is counted in
_TS_MONTH = re.compile(_MONTH)
_FRAG_WHEN = re.compile(r'\{"ts": ?"(' + _MONTH + r'[^"]*)')   # a fragment's timestamp, readable to the month at least


def tokens(o) -> int | None:
    """A whole row's input tokens, or None when `o` is no row. A row is a dict whose `ts` is a string that starts with
    its month (YYYY-MM, month 01-12; else the call would count in no month) and whose count is a finite, non-negative
    number: the writer writes an int; a float is counted rounded up, never down. Any other count (a string, a bool,
    NaN, an infinity, a negative, null or missing) is unknown, so its text is a torn call: never a number that lowers
    the cap's sum or poisons it (NaN compares false)."""
    if not isinstance(o, dict) or not isinstance(o.get("ts"), str) or not _TS_MONTH.match(o["ts"]):
        return None
    t = o.get("input_tokens")
    if isinstance(t, bool) or not isinstance(t, (int, float)) or not 0 <= t < math.inf:
        return None
    return math.ceil(t)


def is_row(o) -> bool:
    return tokens(o) is not None


def now_month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def pieces(line: str) -> list:
    """The whole rows and torn fragments on one meter line, in file order: a row as its dict, a fragment as its text.

    One row per line is the common case. Otherwise (a write cut short, then the next row appended onto the fragment)
    every position where a row starts is decoded with `raw_decode`; a row start that does not decode into a row is a
    fragment's start, and each non-blank piece of text between recovered rows, cut at those starts, is one torn call
    (a row is metered only after the API answered, so each was a billed call). Every write starts with a row start or
    with the newline that ends the line before, so text before a row start is an earlier write's: a call of its own. A
    NUL run (what a crash leaves where data never reached the disk) is at least one call, counted as one: the zero-fill
    is page-sized and can hide several rows, which are then under-counted."""
    try:
        o = json.loads(line)
    except (ValueError, RecursionError):     # RecursionError: deeply nested junk; never a reason to stop the meter
        o = None
    if is_row(o):
        return [o]
    out: list = []

    def junk(a: int, b: int) -> None:
        cuts, i = [a], line.find(ROW_START, a + 1, b)
        while i != -1:
            cuts.append(i)
            i = line.find(ROW_START, i + 1, b)
        out.extend(line[x:y] for x, y in zip(cuts, cuts[1:] + [b]) if line[x:y].strip())

    dec, cur, at = json.JSONDecoder(), 0, line.find(ROW_START)
    while at != -1:
        try:
            o, end = dec.raw_decode(line, at)
        except (ValueError, RecursionError):
            o, end = None, at
        if is_row(o):
            junk(cur, at)
            out.append(o)
            cur, at = end, line.find(ROW_START, end)
        else:
            at = line.find(ROW_START, at + 1)
    junk(cur, len(line))
    return out


def tally(text: str, month: str, now: str) -> list:
    """[calls, input_tokens, torn, max_call_tokens] of `month` in `text` (whole meter lines), read in month `now`. A
    torn call counts in the month of its own timestamp when that is readable; else in the month of the next whole row
    (the meter is in time order, so the fragment is no later than that row: at a month boundary it is counted late, in
    the month the cap is checking, never early, where the cap would miss it); with no whole row after it in this read,
    in `now`, the month of the read (it was written no later than the read), so in one month only: the current one,
    which the cap checks. A blank line is no call."""
    items = []                                   # (timestamp or None, input tokens or None for a torn call)
    for line in text.split("\n"):                # "\n" only: the writer's own line end, never another separator
        if line.strip():
            for x in pieces(line):
                if isinstance(x, dict):
                    items.append((x["ts"], tokens(x)))
                else:
                    w = _FRAG_WHEN.match(x.lstrip())
                    items.append((w.group(1) if w else None, None))
    calls = total = torn = top = 0
    nxt = now
    for when, tok in reversed(items):
        if tok is not None:
            nxt = when
        elif when is None:
            when = nxt
        if when is not None and not when.startswith(month):
            continue
        calls += 1
        if tok is None:
            torn += 1
        else:
            total += tok
            top = max(top, tok)
    return [calls, total, torn, top]


def month_usage(path, month: str | None = None) -> dict:
    """A month's metered calls (whole rows and torn ones), the input tokens of the whole rows, the torn count (calls
    whose tokens are unknown) and the largest whole call, which the cap check charges for each torn one.

    The cache lives as long as the process. It counts the meter again from the start when the file is another file
    (device + inode: replaced, e.g. by a rename), shorter than the offset, or the last `_TAIL` bytes before the offset
    changed (an edit in place that moved or removed rows); an in-place edit that keeps the file, its length up to the
    offset and those bytes is not seen (rename a new file over the meter, or restart the process, after a hand edit).
    A torn piece with no timestamp of its own and no whole row after it counts in the month of the read (`tally`); a
    cache keeps that: when the month turns before a row follows the piece, a past month's cached figures still hold
    it, while every fresh read and the cap (this month only) count it once."""
    now = now_month()
    month = month or now
    p = Path(path)
    with LOCK:
        m = _CACHE.setdefault((str(p), month), [0, 0, 0, 0, 0, None, b""])
        if p.exists():
            with p.open("rb") as fh:
                st = os.fstat(fh.fileno())
                ident = (st.st_dev, st.st_ino)
                stale = m[5] != ident or st.st_size < m[0]
                if not stale and m[6]:
                    fh.seek(m[0] - len(m[6]))
                    stale = fh.read(len(m[6])) != m[6]
                if stale:                    # another file, truncated or edited: count again from the start
                    m[:] = [0, 0, 0, 0, 0, ident, b""]
                fh.seek(m[0])
                chunk = fh.read()
            end = chunk.rfind(b"\n") + 1     # only whole lines; a line being written is picked up next time
            calls, toks, torn, top = tally(chunk[:end].decode("utf-8", "replace"), month, now)
            m[:] = [m[0] + end, m[1] + calls, m[2] + toks, m[3] + torn, max(m[4], top), ident,
                    (m[6] + chunk[:end])[-_TAIL:]]
        else:
            m[:] = [0, 0, 0, 0, 0, None, b""]
        return {"month": month, "calls": m[1], "input_tokens": m[2], "torn": m[3], "max_call_tokens": m[4]}


def append_row(path, row: dict) -> None:
    """One meter row as one write. A meter whose last byte is not a newline ends in a torn row: the new row starts a
    line of its own, never joined to the fragment. The thread lock is the caller's; across processes the tail check
    and the write are not one step, so a race can still join a row to a fragment cut in between (or leave a blank
    line): the reader recovers both."""
    line = (json.dumps(row) + "\n").encode("utf-8")
    with Path(path).open("a+b") as fh:
        size = fh.seek(0, os.SEEK_END)
        if size:
            fh.seek(size - 1)
            if fh.read(1) != b"\n":
                line = b"\n" + line
        fh.write(line)


def share(path, prefix: str, price_usd_per_mtok_input: float, month: str | None = None) -> dict:
    """A month's whole rows whose `purpose` starts with `prefix`: one caller's share of the one meter. A line that is
    no JSON, or a row with no readable count, is skipped here (torn calls are charged by the client's cap, which every
    request passes first)."""
    month = month or now_month()
    p = Path(path)
    calls = toks = 0
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if not isinstance(r, dict):
                continue
            if str(r.get("ts", "")).startswith(month) and str(r.get("purpose", "")).startswith(prefix):
                try:
                    n = int(r.get("input_tokens") or 0)
                except (TypeError, ValueError, OverflowError):
                    continue
                calls += 1
                toks += n
    return {"month": month, "calls": calls, "input_tokens": toks,
            "usd": round(toks / 1e6 * price_usd_per_mtok_input, 6)}
