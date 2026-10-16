"""The reader: a torn write (a full disk, a crash) cuts a row short and loses its newline; an older writer then glued
the next row onto it, so one line held a fragment and a whole row and was no JSON. A strict reader stopped every
caller. Wanted: the whole rows on such a line are recovered and each fragment is counted as a torn call whose tokens
are unknown. All lines here are synthetic."""
import json
import os

from conftest import MODEL
from jev_strict import meter


def row(ts: str, n, purpose: str = "demo:check", **kw) -> str:
    return json.dumps({"ts": ts, "purpose": purpose, "model": MODEL, "input_tokens": n, **kw})


BEFORE = row("2025-03-08T10:00:00Z", 4000, output_tokens=40, questions=3, usage_reported=True)
FRAGMENT = '{"ts": "2025-03-08T10:05:00Z", "purpose": "demo:check", "model": "%s"' % MODEL
JOINED_ROW = row("2025-03-08T10:09:00Z", 2500, "demo:sort", output_tokens=60, questions=4, usage_reported=True)
AFTER = row("2025-03-08T10:15:00Z", 2500, "demo:sort", output_tokens=60, questions=4, usage_reported=True)


def test_a_joined_line_is_read_as_one_whole_row_and_one_torn_call(tmp_path):
    m = tmp_path / "usage.jsonl"
    m.write_text("\n".join([BEFORE, FRAGMENT + JOINED_ROW, AFTER]) + "\n", encoding="utf-8")
    u = meter.month_usage(m, "2025-03")
    assert u["calls"] == 4 and u["torn"] == 1                          # three whole rows and the torn call
    assert u["input_tokens"] == 4000 + 2500 + 2500                     # the joined row's tokens are kept
    assert u["max_call_tokens"] == 4000
    assert meter.month_usage(m, "2025-02") == {"month": "2025-02", "calls": 0, "input_tokens": 0, "torn": 0,
                                               "max_call_tokens": 0}


def test_a_joined_line_is_picked_up_incrementally(tmp_path):
    """The meter is read from the last offset on: a fragment without its newline is not a line yet; once an older
    writer glues the next row onto it, the next read recovers that row and counts the fragment."""
    m = tmp_path / "usage.jsonl"
    m.write_text(BEFORE + "\n" + FRAGMENT, encoding="utf-8")
    assert meter.month_usage(m, "2025-03") == {"month": "2025-03", "calls": 1, "input_tokens": 4000, "torn": 0,
                                               "max_call_tokens": 4000}
    with m.open("a", encoding="utf-8") as fh:                           # what a writer without the newline rule did
        fh.write(JOINED_ROW + "\n")
    u = meter.month_usage(m, "2025-03")
    assert (u["calls"], u["input_tokens"], u["torn"]) == (3, 4000 + 2500, 1)


def test_torn_fragments_are_counted_and_attributed_to_a_month(tmp_path, monkeypatch):
    """A fragment's month is its own timestamp when that is readable; else the month of the next whole row (the file
    is in time order, so the fragment is no later than it: never an earlier month, which the cap would miss); with no
    whole row after it in the same read, the month of the read. Several cut rows on one line are each a torn call; a
    blank line (two writers both starting a new line after a fragment) is no call."""
    monkeypatch.setattr(meter, "now_month", lambda: "2025-10")         # the read happens in October
    m = tmp_path / "usage.jsonl"
    lines = [
        row("2025-09-30T23:59:58Z", 100, "t"),
        '{"ts": "2025-09-30T23:59:59Z", "purpose": "x"',                # torn, its month readable: September
        '{"t',                                                          # torn, no month: the next row's (October)
        row("2025-10-01T00:00:00Z", 200, "t"),
        "",
        '{"ts": "2025-10-02T00:00:00Z", "purpose": "a"' + '{"ts": "2025-10-02T00:00:01Z", "purp'
        + row("2025-10-02T00:00:02Z", 300, "t"),
        '{"ts": "20',                                                   # torn, no month, nothing after it
    ]
    m.write_text("\n".join(lines) + "\n", encoding="utf-8")
    sep, octo = meter.month_usage(m, "2025-09"), meter.month_usage(m, "2025-10")
    assert (sep["calls"], sep["input_tokens"], sep["torn"]) == (2, 100, 1)    # row, readable fragment
    assert (octo["calls"], octo["input_tokens"], octo["torn"]) == (6, 500, 4)  # 2 rows; '{"t', two cut rows, trailing
    with m.open("a", encoding="utf-8") as fh:
        fh.write(row("2025-11-01T00:00:00Z", 50, "t") + "\n")
    nov = meter.month_usage(m, "2025-11")                               # a fresh read: the trailing one has a next row
    assert (nov["calls"], nov["input_tokens"], nov["torn"]) == (2, 50, 1)


def test_a_whole_row_with_a_float_count_is_a_row(tmp_path):
    """A finite, non-negative number is a row's count, a fraction rounded up (never down); a count that is no such
    number (a string, a bool, NaN, an infinity, a negative, null) is unknown, so its line is a torn call in its own
    month, never a count that lowers the cap's sum or poisons it (NaN compares false)."""
    m = tmp_path / "usage.jsonl"
    lines = ['{"ts": "2025-03-01T00:00:00Z", "purpose": "t", "input_tokens": 1000.0}',
             '{"ts": "2025-03-01T00:00:01Z", "purpose": "t", "input_tokens": 2.5}',
             '{"ts": "2025-03-01T00:00:02Z", "purpose": "t", "input_tokens": 7}']
    lines += ['{"ts": "2025-03-02T00:00:%02dZ", "purpose": "t", "input_tokens": %s}' % (i, v)
              for i, v in enumerate(('"1000"', "true", "NaN", "Infinity", "-5", "null"))]
    m.write_text("\n".join(lines) + "\n", encoding="utf-8")
    u = meter.month_usage(m, "2025-03")
    assert u == {"month": "2025-03", "calls": 9, "input_tokens": 1000 + 3 + 7, "torn": 6, "max_call_tokens": 1000}
    assert type(u["input_tokens"]) is int and type(u["max_call_tokens"]) is int


def test_an_undated_fragment_with_nothing_after_it_counts_in_one_month(tmp_path):
    """A torn piece with no readable timestamp and no whole row after it was written no later than the read: it
    counts in the month of the read only (the current month, the one the cap checks), once."""
    m = tmp_path / "usage.jsonl"
    now = meter.now_month()
    y, mo = map(int, now.split("-"))
    before, after = f"{y - (mo == 1)}-{(mo - 2) % 12 + 1:02d}", f"{y + (mo == 12)}-{mo % 12 + 1:02d}"
    m.write_text(row(now + "-01T00:00:00Z", 100, "t") + "\n" + '{"ts": "20' + "\n", encoding="utf-8")
    got = {k: meter.month_usage(m, k) for k in (before, now, after)}
    assert (got[now]["calls"], got[now]["torn"]) == (2, 1)
    assert (got[before]["calls"], got[before]["torn"], got[after]["calls"], got[after]["torn"]) == (0, 0, 0, 0)


def test_a_row_whose_ts_names_no_month_is_a_torn_call(tmp_path):
    """A whole row whose ts names no month ("yesterday", month 13) would count in no month, and an undated torn piece
    before it would take that ts and count in no month either: a call lost to the cap. Wanted: a row's ts starts with
    its month (YYYY-MM, month 01-12), as a fragment's must; a row without one is a torn call."""
    m = tmp_path / "usage.jsonl"
    lines = [
        row("2025-03-01T00:00:00Z", 100, "t"),
        '{"t',                                                          # torn, undated: the next whole row's month
        '{"ts": "yesterday", "purpose": "t", "input_tokens": 50}',      # parseable, but its ts names no month
        '{"ts": "2025-13-01T00:00:00Z", "purpose": "t", "input_tokens": 70}',     # month 13: no month either
        '{"ts": "2025-13-01T00:00:01Z", "purp',                         # a fragment whose ts names no month
        row("2025-03-02T00:00:00Z", 200, "t"),
    ]
    m.write_text("\n".join(lines) + "\n", encoding="utf-8")
    u = meter.month_usage(m, "2025-03")
    assert (u["calls"], u["input_tokens"], u["torn"], u["max_call_tokens"]) == (6, 300, 4, 200)


def test_a_meter_rewritten_in_place_is_counted_again(tmp_path):
    """A long-lived process keeps one incremental cache. A hand edit that rewrites the meter in place (same file) and
    leaves it no shorter than the cached offset must not be read on from that offset, mid-file: the reader notices
    that the bytes before its offset changed and counts the file again from the start."""
    m = tmp_path / "usage.jsonl"
    a, b, c, d, e = (row(f"2025-03-0{i}T00:00:00Z", n, "t") for i, n in enumerate((100, 200, 300, 400, 500), 1))
    m.write_text("\n".join([a, b, c]) + "\n", encoding="utf-8")
    ino = m.stat().st_ino
    assert meter.month_usage(m, "2025-03")["input_tokens"] == 600
    m.write_text("\n".join([a, c, d, e]) + "\n", encoding="utf-8")       # b cut, two rows after it: longer
    assert m.stat().st_ino == ino                                       # the same file, edited in place
    assert meter.month_usage(m, "2025-03") == {"month": "2025-03", "calls": 4, "input_tokens": 1300, "torn": 0,
                                               "max_call_tokens": 500}


def test_a_meter_replaced_by_a_new_file_is_counted_again(tmp_path):
    """An edit made by writing a new file and renaming it over the meter, same size, the change far before the
    offset: a size check alone would keep the old tallies. Wanted: a new file is counted again from the start."""
    m = tmp_path / "usage.jsonl"
    rows = [row(f"2025-03-01T00:00:{i:02d}Z", 100, "t") for i in range(12)]   # the edit lies > 512 bytes before the end
    m.write_text("\n".join(rows) + "\n", encoding="utf-8")
    assert meter.month_usage(m, "2025-03")["input_tokens"] == 1200
    new = tmp_path / "usage.jsonl.new"
    new.write_text("\n".join([rows[0].replace('"input_tokens": 100', '"input_tokens": 900')] + rows[1:]) + "\n",
                   encoding="utf-8")
    os.replace(new, m)                                                  # same size, a new file
    assert meter.month_usage(m, "2025-03")["input_tokens"] == 2000


def test_a_share_counts_one_purpose_prefix_and_skips_what_it_cannot_read(tmp_path):
    m = tmp_path / "usage.jsonl"
    m.write_text("\n".join([row("2025-03-01T00:00:00Z", 1000, "judge:a"), row("2025-03-01T00:00:01Z", 10 ** 6, "other"),
                            FRAGMENT, "7", row("2025-03-01T00:00:02Z", "x", "judge:b"),
                            row("2025-02-28T00:00:00Z", 50, "judge:a")]) + "\n", encoding="utf-8")
    assert meter.share(m, "judge:", 0.5, "2025-03") == {"month": "2025-03", "calls": 1, "input_tokens": 1000,
                                                        "usd": 0.0005}
