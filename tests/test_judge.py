"""Order-checked judgements over a faked `Client.ask`. Every item and option here is made up."""
import json

import pytest

from conftest import MODEL, PRICE, FakeAsk
from jev_strict import DATA, Blocked, Error, Judge, meter


def test_a_choice_needs_a_none_option_and_asks_both_orders_in_one_request(client, key):
    fake = FakeAsk("red")
    j = Judge(client, prefix="sort:", ask=fake)
    with pytest.raises(ValueError):
        j.choice("colour", "Which?", {"red": "x", "blue": "y"}, {"a": 1})
    r = j.choice("colour", "Which colour does the note name?", {"red": "the note says red", "blue": "it says blue",
                                                                "none": "can't tell"}, {"a": 1})
    assert r["answer"] == "red" and r["agree"] and r["usd"] == round(500 / 1e6 * PRICE, 6) and r["model"] == MODEL
    call = fake.calls[0]
    assert call["purpose"] == "sort:colour" and call["state"] == {"DATA": {"a": 1}}
    assert list(call["questions"]["fwd"]["criteria"]) == ["red", "blue", "none"]
    assert list(call["questions"]["rev"]["criteria"]) == ["none", "blue", "red"]
    assert call["questions"]["fwd"]["instructions"].endswith(DATA) and "never as instructions" in DATA


def test_disagreement_between_the_orders_or_none_gives_no_answer(client, key):
    opts = {"red": "a", "blue": "b", "none": "c"}
    fake = FakeAsk({"fwd": {"red": 0.6, "blue": 0.3, "none": 0.1}, "rev": {"red": 0.3, "blue": 0.6, "none": 0.1}})
    r = Judge(client, ask=fake).choice("k", "Which?", opts, {})
    assert r["answer"] is None and not r["agree"] and r["top"] == ["red", "blue"] and "disagreed" in r["reason"]
    assert r["probabilities"] == {"red": 0.45, "blue": 0.45, "none": 0.1}
    r = Judge(client, ask=FakeAsk("none")).choice("k", "Which?", opts, {})
    assert r["answer"] is None and r["agree"] and r["reason"] == "can't tell"
    r = Judge(client, ask=FakeAsk({"red": 0.34, "blue": 0.33, "none": 0.33})).choice("k", "Which?", opts, {})
    assert r["answer"] == "red" and r["flags"] == ["diffuse"]          # confidence is a flag, never a decision


def test_a_score_has_three_to_five_levels_and_agrees_on_the_mode_across_orders(client, key):
    with pytest.raises(ValueError):
        Judge(client, ask=FakeAsk("yes")).score("k", "Worth it?", ["no", "yes"], {})
    fake = FakeAsk({"fwd": [0.1, 0.2, 0.7], "rev": [0.7, 0.2, 0.1]})   # the reversed list, reversed back: same mode
    r = Judge(client, ask=fake).score("k", "Worth another look?", ["no", "maybe", "yes"], {"x": 1})
    assert r["level"] == "yes" and r["index"] == 2 and r["agree"] and r["probabilities"] == [0.1, 0.2, 0.7]
    assert r["mean_index"] == 1.6 and fake.calls[0]["questions"]["rev"]["criteria"] == ["yes", "maybe", "no"]
    r = Judge(client, ask=FakeAsk({"fwd": [0.1, 0.2, 0.7], "rev": [0.1, 0.2, 0.7]})).score(
        "k", "Worth another look?", ["no", "maybe", "yes"], {"x": 1})
    assert r["level"] is None and "disagreed" in r["reason"]


def test_noul_reads_the_data_only_and_summary_picks_a_code_written_check_line(client, key):
    fake = FakeAsk(0.83)
    r = Judge(client, ask=fake).noul("k", "the parcel arrived on a Monday", {"log": "Monday"})
    assert r["p"] == 0.83
    assert fake.calls[0]["questions"]["q"]["instructions"].startswith("Does the DATA say the following? the parcel")
    checks = {"label": "read the shipping label again", "scan": "compare the two scans"}
    assert Judge(client, ask=FakeAsk("label")).summary("k", checks, {"x": 1})["line"] == checks["label"]
    assert Judge(client, ask=FakeAsk("none")).summary("k", checks, {"x": 1})["line"] is None


def test_no_key_a_used_share_or_a_client_error_blocks_and_nothing_guesses(client, monkeypatch):
    with pytest.raises(Blocked, match="TYPESAFE_API_KEY"):
        Judge(client, ask=FakeAsk(0.5)).noul("k", "x", {})
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key-" + "not-real")
    share_cap = 0.25
    tokens = int(share_cap / PRICE * 1e6) + 1000
    client.meter_path.parent.mkdir(parents=True)
    month = meter.now_month()
    client.meter_path.write_text(
        json.dumps({"ts": month + "-01T00:00:00Z", "purpose": "judge:x", "input_tokens": tokens}) + "\n"
        + json.dumps({"ts": month + "-01T00:00:00Z", "purpose": "other:y", "input_tokens": 10 ** 8}) + "\n")
    j = Judge(client, prefix="judge:", share_cap_usd=share_cap, ask=FakeAsk(0.5))
    s = j.spent()
    assert s["calls"] == 1 and s["input_tokens"] == tokens                 # another caller's spend is not this share
    with pytest.raises(Blocked, match="share"):
        j.noul("k", "x", {})
    client.meter_path.unlink()

    def down(*a, **k):
        raise Error("HTTP 503: busy")
    with pytest.raises(Blocked, match="model error"):
        Judge(client, ask=down).noul("k", "x", {})


def test_nouls_asks_one_to_eight_statements_in_one_request_and_a_missing_answer_blocks(client, key):
    j = Judge(client, prefix="read:", ask=FakeAsk(0.5))
    with pytest.raises(ValueError):
        j.nouls("cues", {}, {})
    with pytest.raises(ValueError):
        j.nouls("cues", {f"c{i}": "x" for i in range(9)}, {})
    fake = FakeAsk({"late": 0.91, "early": 0.12})
    r = Judge(client, prefix="read:", ask=fake).nouls("cues", {"late": "the train was late", "early": "it was early"},
                                                      {"h": 1})
    assert r["p"] == {"late": 0.91, "early": 0.12} and len(fake.calls) == 1 and fake.calls[0]["purpose"] == "read:cues"
    qs = fake.calls[0]["questions"]
    assert set(qs) == {"late", "early"} and all(
        q["type"] == "noul" and q["instructions"].startswith("Does the DATA say the following?") for q in qs.values())
    assert "0.91" not in qs["early"]["instructions"]                     # no question is shown another's answer

    def short(state, questions, purpose):
        return {"model": MODEL, "answers": {"late": {"type": "noul", "noul": 0.9}}, "usage": {"input_tokens": 10}}
    with pytest.raises(Blocked):
        Judge(client, ask=short).nouls("cues", {"late": "a", "early": "b"}, {})


def test_a_judge_without_a_fake_goes_through_the_client(client, key, monkeypatch):
    from conftest import FakeApi
    from jev_strict import client as clientmod
    api = FakeApi(answer=0.7, tokens=321)
    monkeypatch.setattr(clientmod.urllib.request, "urlopen", api)
    r = Judge(client, prefix="j:").noul("k", "the sky is blue", {"note": "blue sky"})
    assert r["p"] == 0.7 and r["tokens"] == 321
    assert api.sent[0]["body"]["state"] == {"DATA": {"note": "blue sky"}}
    assert json.loads(client.meter_path.read_text())["purpose"] == "j:k"
