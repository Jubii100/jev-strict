"""The client: the pin, the key, the writer, the cap and status. No network: urlopen is faked."""
import io
import json
import urllib.error

import pytest

from conftest import CAP, MODEL, PRICE, FakeApi, Resp
from jev_strict import Client, Error, Unavailable, client as clientmod, meter, torn_note
from test_meter import BEFORE, FRAGMENT, JOINED_ROW

Q = {"q": {"type": "noul", "instructions": "?"}}


def moved(line: str, month: str) -> str:
    """The same line with its timestamps moved into `month` (status and the cap read the current month)."""
    return line.replace("2025-03-", month + "-")


def at_cap_tokens() -> int:
    return int(CAP / PRICE * 1e6)


# ------------------------------------------------------------------ the key and the pin
def test_no_key_is_blocked_live_and_a_capped_month_refuses_before_any_request(client, monkeypatch):
    assert client.status()["status"].startswith("BLOCKED-LIVE")
    with pytest.raises(Unavailable, match="BLOCKED-LIVE"):
        client.ask("s", Q, "t")
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key-" + "not-real")
    client.meter_path.parent.mkdir(parents=True)
    client.meter_path.write_text(json.dumps({"ts": meter.now_month() + "-01T00:00:00Z", "input_tokens": at_cap_tokens()})
                                 + "\n")
    monkeypatch.setattr(clientmod.urllib.request, "urlopen",
                        lambda *a, **k: pytest.fail("a request left although the cap was reached"))
    with pytest.raises(Unavailable, match="cap"):
        client.ask("s", Q, "t")


def test_another_model_is_an_error_and_the_call_is_metered_without_the_key(client, key, monkeypatch):
    api = FakeApi(model="jev-9.9.9", tokens=123)
    monkeypatch.setattr(clientmod.urllib.request, "urlopen", api)
    with pytest.raises(Error, match="pinned"):
        client.ask("s", Q, "t")
    assert api.sent[0]["body"]["model"] == MODEL and api.sent[0]["auth"].startswith("Bearer ")
    use = client.meter_path.read_text()
    assert '"input_tokens": 123' in use and "not-real" not in use
    assert "not-real" not in repr(client)


def test_a_missing_answer_is_an_error(client, key, monkeypatch):
    def short(req, timeout):                                            # answers q, never q2
        return Resp({"model": MODEL, "answers": {"q": {"type": "noul", "noul": 0.5}}, "usage": {"input_tokens": 9}})
    monkeypatch.setattr(clientmod.urllib.request, "urlopen", short)
    with pytest.raises(Error, match="no answer"):
        client.ask("s", {**Q, "q2": Q["q"]}, "t")
    assert client.ask("s", Q, "t")["answers"]["q"]["noul"] == 0.5


def test_the_alias_check_reads_only_the_served_model(client, key, monkeypatch):
    served = {"model": MODEL}
    sent = []

    def fake(req, timeout):
        sent.append(json.loads(req.data))
        return FakeApi(model=served["model"], tokens=280)(req, timeout)

    monkeypatch.setattr(clientmod.urllib.request, "urlopen", fake)
    assert client.alias_target() == {"alias": "jev-latest", "serves": MODEL, "pinned": MODEL, "drift": False}
    served["model"] = "jev-99.0.0"
    assert client.alias_target()["drift"] is True
    served["model"] = "jev-latest"                                     # the alias echoed back: the target is not visible
    assert client.alias_target()["drift"] is None
    assert all(b["model"] == "jev-latest" and "target" not in json.dumps(b["state"]) for b in sent)   # content-free
    use = client.meter_path.read_text().splitlines()
    assert len(use) == 3 and all('"purpose": "alias-check"' in u for u in use)                # metered like any call


def test_the_alias_is_never_the_pin(tmp_path):
    with pytest.raises(ValueError):
        Client("jev-latest", "https://api.invalid", price_usd_per_mtok_input=PRICE, monthly_cap_usd=CAP,
               meter_path=tmp_path / "m.jsonl")


def test_the_key_is_read_from_an_env_file_with_quotes_stripped(tmp_path, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    env = tmp_path / ".env"
    env.write_text('OTHER=1\nTYPESAFE_API_KEY="test-key-' + 'quoted"\n', encoding="utf-8")
    c = Client(MODEL, "https://api.invalid", price_usd_per_mtok_input=PRICE, monthly_cap_usd=CAP,
               meter_path=tmp_path / "m.jsonl", env_file=env)
    assert c.has_key() and c._key() == "test-key-" + "quoted"
    env.write_text("TYPESAFE_API_KEY=\n", encoding="utf-8")
    assert not c.has_key()                                              # blank is unset, never an empty bearer


def test_a_busy_reply_is_retried_and_a_refusal_is_not(client, key, monkeypatch):
    calls, waits = [], []
    monkeypatch.setattr(clientmod.time, "sleep", waits.append)

    def flaky(code):
        def fake(req, timeout):
            calls.append(code)
            if len(calls) < 3:
                raise urllib.error.HTTPError(req.full_url, code, "busy", {}, io.BytesIO(b"busy"))   # a body, as urlopen's
            return FakeApi()(req, timeout)
        return fake

    monkeypatch.setattr(clientmod.urllib.request, "urlopen", flaky(503))
    assert client.ask("s", Q, "t")["model"] == MODEL and len(calls) == 3 and waits == [1.5, 3.0]
    calls.clear()
    monkeypatch.setattr(clientmod.urllib.request, "urlopen", flaky(401))
    with pytest.raises(Error, match="HTTP 401"):
        client.ask("s", Q, "t")
    assert len(calls) == 1                                              # a refusal is not retried
    assert len(client.meter_path.read_text().splitlines()) == 1         # only the answered call is metered


# ------------------------------------------------------------------ the writer
def test_the_writer_starts_a_new_line_after_a_fragment(client, key, monkeypatch):
    api = FakeApi(tokens=2500)
    monkeypatch.setattr(clientmod.urllib.request, "urlopen", api)
    client.meter_path.parent.mkdir(parents=True)
    client.meter_path.write_text(BEFORE + "\n" + FRAGMENT, encoding="utf-8")     # the meter as a full disk left it
    client.ask("s", Q, "t")
    client.ask("s", Q, "t")                                             # and a row after a clean end: no blank line
    lines = client.meter_path.read_text(encoding="utf-8").split("\n")
    assert lines[:2] == [BEFORE, FRAGMENT] and lines[-1] == "" and len(lines) == 5, lines
    assert all(json.loads(x)["input_tokens"] == 2500 for x in lines[2:4])        # each new row is a line of its own
    assert len(api.sent) == 2 and meter.month_usage(client.meter_path, "2025-03")["torn"] == 1


def test_the_writer_adds_no_newline_to_an_empty_or_clean_meter(client, key, monkeypatch):
    monkeypatch.setattr(clientmod.urllib.request, "urlopen", FakeApi())
    client.ask("s", Q, "t")
    text = client.meter_path.read_text(encoding="utf-8")
    assert text.count("\n") == 1 and text.endswith("}\n") and not text.startswith("\n")


# ------------------------------------------------------------------ the cap never reads unknown as 0
def test_a_torn_call_is_charged_at_the_months_largest_call(client, key, monkeypatch):
    month = meter.now_month()
    big = at_cap_tokens() // 2 + 1000                                   # one whole call: half the cap and a little more
    whole = json.dumps({"ts": month + "-01T00:00:00Z", "purpose": "t", "model": MODEL, "input_tokens": big})
    api = FakeApi(tokens=1)
    monkeypatch.setattr(clientmod.urllib.request, "urlopen", api)
    client.meter_path.parent.mkdir(parents=True)
    client.meter_path.write_text(whole + "\n", encoding="utf-8")
    client.ask("s", Q, "t")                                             # control: under the cap, the request leaves
    assert len(api.sent) == 1
    with client.meter_path.open("a", encoding="utf-8") as fh:           # a torn call this month, on a line of its own
        fh.write(moved(FRAGMENT, month) + "\n")
    monkeypatch.setattr(clientmod.urllib.request, "urlopen",
                        lambda *a, **k: pytest.fail("a request left although the cap was reached"))
    with pytest.raises(Unavailable, match="cap"):
        client.ask("s", Q, "t")                                         # big + big (the torn call) passes the cap
    u = client.month_usage()
    assert (u["calls"], u["torn"], u["input_tokens"], u["max_call_tokens"]) == (3, 1, big + 1, big)


# ------------------------------------------------------------------ what the operator reads
def test_status_shows_the_torn_count_and_the_cap_basis(client):
    month = meter.now_month()
    client.meter_path.parent.mkdir(parents=True)
    client.meter_path.write_text("\n".join(moved(x, month) for x in (BEFORE, FRAGMENT + JOINED_ROW)) + "\n",
                                 encoding="utf-8")
    out = client.status()
    assert out["torn"] == 1 and out["calls"] == 3 and out["input_tokens"] == 4000 + 2500
    assert out["torn_charge_tokens"] == 4000
    assert out["month_usd"] == round((4000 + 2500) / 1e6 * PRICE, 6)
    assert out["cap_basis_usd"] == round((4000 + 2500 + 4000) / 1e6 * PRICE, 6) > out["month_usd"]
    assert "1 torn meter row" in torn_note(out) and "cap basis" in torn_note(out)


def test_a_torn_charge_with_no_whole_call_this_month_is_unknown_never_0(client):
    client.meter_path.parent.mkdir(parents=True)
    client.meter_path.write_text(moved(FRAGMENT, meter.now_month()) + "\n", encoding="utf-8")
    out = client.status()
    assert out["torn"] == 1 and out["calls"] == 1 and out["max_call_tokens"] == 0
    assert out["torn_charge_tokens"] is None and out["cap_basis_usd"] is None
    note = torn_note(out)
    assert "1 torn meter row(s) of unknown size" in note and "cap basis unknown" in note


def test_the_torn_note_states_the_cap_rule_and_never_denies_a_whole_call(client):
    """The cap check charges each torn row the larger of the month's largest call and the request's own estimate, so
    the cap basis shown is the floor of what a request charges; with whole calls of 0 tokens only, the note says no
    whole call above 0 tokens, never "no whole call"."""
    month = meter.now_month()
    client.meter_path.parent.mkdir(parents=True)
    client.meter_path.write_text("\n".join(moved(x, month) for x in (BEFORE, FRAGMENT)) + "\n", encoding="utf-8")
    s = client.status()
    assert (s["calls"], s["torn"], s["torn_charge_tokens"]) == (2, 1, 4000)
    note = torn_note(s)
    assert f"cap basis {s['cap_basis_usd']} USD" in note and "its own estimate" in note, note
    zero = json.dumps({"ts": month + "-01T00:00:00Z", "purpose": "t", "model": MODEL, "input_tokens": 0})
    client.meter_path.write_text(zero + "\n" + moved(FRAGMENT, month) + "\n", encoding="utf-8")
    s = client.status()                                                 # rewritten shorter: counted again
    assert (s["calls"], s["torn"], s["max_call_tokens"], s["cap_basis_usd"]) == (2, 1, 0, None)
    note = torn_note(s)
    assert "cap basis unknown" in note and "no whole call above 0 tokens" in note, note
    assert "no whole call this month" not in note, note                 # there is one, of 0 tokens
    assert torn_note({"torn": 0}) == ""
