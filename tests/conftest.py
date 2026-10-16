import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jev_strict import Client  # noqa: E402

MODEL = "jev-0.0.0-test"
PRICE = 0.5          # USD per million input tokens: a made-up figure, not the vendor's price
CAP = 1.0            # USD a month: the cap is reached at 2,000,000 input tokens


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)       # never the host's key
    return Client(MODEL, "https://api.invalid/v1/evaluate", price_usd_per_mtok_input=PRICE, monthly_cap_usd=CAP,
                  meter_path=tmp_path / "meter" / "usage.jsonl", timeout_s=5)


@pytest.fixture()
def key(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key-" + "not-real")


class Resp:
    def __init__(self, body):
        self.body = body

    def read(self):
        return json.dumps(self.body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeApi:
    """Stands in for urllib.request.urlopen: answers every question with a noul, records every request."""

    def __init__(self, model=MODEL, tokens=2500, answer=0.5):
        self.model, self.tokens, self.answer, self.sent = model, tokens, answer, []

    def __call__(self, req, timeout):
        body = json.loads(req.data)
        self.sent.append({"body": body, "auth": req.get_header("Authorization")})
        return Resp({"model": self.model, "answers": {q: {"type": "noul", "noul": self.answer} for q in body["questions"]},
                     "usage": {"input_tokens": self.tokens, "output_tokens": 3}})


class FakeAsk:
    """Stands in for Client.ask: answers per question id from a plan; records every request."""

    def __init__(self, plan, model=MODEL):
        self.plan, self.model, self.calls = plan, model, []

    def __call__(self, state, questions, purpose):
        self.calls.append({"state": state, "questions": questions, "purpose": purpose})
        answers = {}
        for qid, q in questions.items():
            spec = self.plan[qid] if isinstance(self.plan, dict) and qid in self.plan else self.plan
            if q["type"] == "choice":
                probs = spec if isinstance(spec, dict) else {
                    k: (0.9 if k == spec else 0.1 / max(1, len(q["criteria"]) - 1)) for k in q["criteria"]}
                top = max(probs, key=probs.get)
                n = len(probs)
                answers[qid] = {"type": "choice", "choice": top, "probabilities": probs,
                                "confidence": round((probs[top] - 1 / n) / (1 - 1 / n), 3)}
            elif q["type"] == "score":
                levels = q["criteria"]
                probs = spec if isinstance(spec, list) else [0.8 if lv == spec else 0.2 / (len(levels) - 1) for lv in levels]
                answers[qid] = {"type": "score", "score": sum(i * x for i, x in enumerate(probs)),
                                "legend": {str(i): lv for i, lv in enumerate(levels)},
                                "probabilities": {str(i): x for i, x in enumerate(probs)},   # the API's shape: by index
                                "confidence": 0.7}
            else:
                answers[qid] = {"type": "noul", "noul": float(spec)}
        return {"model": self.model, "answers": answers, "usage": {"input_tokens": 500, "output_tokens": 0}}
