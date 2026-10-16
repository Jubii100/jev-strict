"""The client: one pinned model, one meter, one hard monthly cap, no fallback.

Contract (the vendor's API reference at docs.typesafe.ai/api): POST {endpoint} with `Authorization: Bearer <key>`, body
{state, model, questions}; answers come back per question id as noul (`noul`), choice (`choice`, `probabilities`,
`confidence`) or score (`score`, `legend`, `probabilities`, `confidence`), plus `usage`. Output tokens are free; input
is priced.

Rules this client enforces:
  * the model is PINNED; an alias or a response from another model is an error, not a fallback (the one exception is
    `alias_target`, which asks the moving alias a fixed question and reads only its model id);
  * the key is read from the environment or an env file and never printed, logged or stored;
  * every call is metered into one JSONL file, one row per line (`meter`); a call that would pass the monthly cap is
    refused before any request leaves, a torn call charged the larger of the month's largest call and the request's
    own estimate (an estimate, not a ceiling), never 0;
  * no key, or the cap reached => `Unavailable` ("BLOCKED-LIVE"): callers record the model as unavailable and never
    substitute a guess."""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from . import meter

KEY_NAME = "TYPESAFE_API_KEY"
ALIAS = "jev-latest"
RETRY_STATUS = (429, 529, 500, 502, 503, 504)


class Unavailable(Exception):
    """The model cannot be asked now (no key, or the monthly cap would be passed): nothing guesses."""


class Error(Exception):
    """The request failed, or the reply broke the contract (another model, a missing answer)."""


class Client:
    """A metered, capped client for one pinned model.

    `meter_path` is the JSONL file every call is metered into; share it between every process that spends against the
    same cap. `env_file` is read for `key_env` when the environment does not hold it (KEY=value lines; quotes
    stripped). Prices are USD per million input tokens."""

    def __init__(self, model: str, endpoint: str, *, price_usd_per_mtok_input: float, monthly_cap_usd: float,
                 meter_path, timeout_s: float = 60, key_env: str = KEY_NAME, env_file=None, alias: str = ALIAS,
                 user_agent: str = "jev-strict/0.1"):
        if not model or model == alias:
            raise ValueError("pin a model id, never the moving alias")
        self.model, self.endpoint, self.alias = model, endpoint, alias
        self.price = float(price_usd_per_mtok_input)
        self.cap = float(monthly_cap_usd)
        self.meter_path = Path(meter_path)
        self.timeout_s, self.key_env, self.user_agent = timeout_s, key_env, user_agent
        self.env_file = Path(env_file) if env_file else None

    def __repr__(self) -> str:                   # never the key
        return f"Client(model={self.model!r}, endpoint={self.endpoint!r}, meter_path={str(self.meter_path)!r})"

    # ------------------------------------------------------------------ the key
    def _key(self) -> str | None:
        v = os.environ.get(self.key_env)
        if v:
            return v.strip()
        env = self.env_file
        if env and env.exists():
            for line in env.read_text(encoding="utf-8").splitlines():
                if line.startswith(self.key_env + "="):
                    v = line.split("=", 1)[1].strip().strip('"').strip("'")
                    return v or None
        return None

    def has_key(self) -> bool:
        return bool(self._key())

    # ------------------------------------------------------------------ the meter
    def month_usage(self, month: str | None = None) -> dict:
        return meter.month_usage(self.meter_path, month)

    def share(self, prefix: str, month: str | None = None) -> dict:
        """The whole rows of one purpose prefix this month: a caller's share of the one meter."""
        return meter.share(self.meter_path, prefix, self.price, month)

    def status(self) -> dict:
        """`month_usd` is the whole rows' spend; `cap_basis_usd` adds each torn call at the month's largest whole call
        (`torn_charge_tokens`), an estimate, not a ceiling (a torn call may have been larger). With torn calls and no
        whole call above 0 tokens this month both are None: unknown, never 0. The cap check charges a torn call the
        larger of the month's largest call and the request's own estimate, so `cap_basis_usd` is the floor of a
        request's basis."""
        u = self.month_usage()
        torn_tokens = u["torn"] * u["max_call_tokens"] if u["max_call_tokens"] or not u["torn"] else None
        return {"status": "key present" if self._key() else f"BLOCKED-LIVE (no {self.key_env})", "model": self.model,
                **u, "torn_charge_tokens": torn_tokens, "month_usd": round(u["input_tokens"] / 1e6 * self.price, 6),
                "cap_basis_usd": None if torn_tokens is None else
                round((u["input_tokens"] + torn_tokens) / 1e6 * self.price, 6),
                "cap_usd": self.cap}

    # ------------------------------------------------------------------ requests
    def _post(self, model: str, state, questions: dict, purpose: str, retries: int = 3) -> dict:
        """One metered, capped request to `model`. Returns the parsed response; raises Unavailable / Error."""
        key = self._key()
        if not key:
            raise Unavailable(f"BLOCKED-LIVE: {self.key_env} is not set")
        body = json.dumps({"state": state, "model": model, "questions": questions}, ensure_ascii=False).encode("utf-8")
        est_tokens = len(body) // 3 + 200  # deliberate over-estimate for the cap check; the real count is metered below
        u = self.month_usage()
        # A torn call's tokens are unknown, never 0: it is charged the month's largest whole call, or this request's
        # deliberate over-estimate if that is larger. A fixed large charge (say, the whole cap) would turn one torn
        # line into a stop of every caller; the largest call seen is the meter's own evidence of what a call costs.
        # It is an estimate, not a ceiling: a torn call may have been larger than every whole row so far.
        torn_each = max(u["max_call_tokens"], est_tokens)
        charged = u["input_tokens"] + u["torn"] * torn_each + est_tokens
        if charged / 1e6 * self.price > self.cap:
            torn = (f" ({u['torn']} torn meter row(s) of unknown size charged at {torn_each} input tokens each)"
                    if u["torn"] else "")
            raise Unavailable(f"monthly cap of {self.cap} USD would be passed; call refused{torn}")
        req = urllib.request.Request(self.endpoint, data=body, method="POST", headers={
            "Authorization": "Bearer " + key, "Content-Type": "application/json", "User-Agent": self.user_agent})
        last = None
        for attempt in range(retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                    out = json.loads(resp.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:300]
                last = f"HTTP {e.code}: {detail}"
                if e.code not in RETRY_STATUS or attempt == retries:
                    raise Error(last) from None
            except (urllib.error.URLError, TimeoutError) as e:
                last = f"network: {e}"
                if attempt == retries:
                    raise Error(last) from None
            time.sleep(1.5 * (2 ** attempt))
        else:  # pragma: no cover
            raise Error(last or "no response")
        usage = out.get("usage") or {}
        self.meter_path.parent.mkdir(parents=True, exist_ok=True)
        with meter.LOCK:
            meter.append_row(self.meter_path, {
                "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "purpose": purpose,
                "model": out.get("model"), "input_tokens": int(usage.get("input_tokens", est_tokens)),
                "output_tokens": int(usage.get("output_tokens", 0)), "questions": len(questions),
                "usage_reported": "input_tokens" in usage})
        return out

    def ask(self, state, questions: dict, purpose: str, retries: int = 3) -> dict:
        """One evaluation request on the pinned model. Returns the parsed response; raises Unavailable / Error."""
        out = self._post(self.model, state, questions, purpose, retries)
        if out.get("model") != self.model:
            raise Error(f"response came from {out.get('model')!r}, pinned model is {self.model!r}")
        missing = set(questions) - set(out.get("answers", {}))
        if missing:
            raise Error(f"no answer for question(s): {sorted(missing)}")
        return out

    def alias_target(self) -> dict:
        """Which model the vendor's moving alias serves today: one metered request with a fixed, content-free
        question. The answer is discarded; only the echoed model id is read. A target other than the pin means a new
        version exists and every rule measured on the pin is due a re-test. `drift` is None when the vendor echoes the
        alias itself (target not visible)."""
        out = self._post(self.alias, {"note": "version check; the answer is discarded"},
                         {"q": {"type": "noul", "instructions": "Is seven greater than three?"}}, "alias-check",
                         retries=1)
        serves = out.get("model")
        drift = None if not serves or serves == self.alias else serves != self.model
        return {"alias": self.alias, "serves": serves, "pinned": self.model, "drift": drift}


def torn_note(s: dict) -> str:
    """Words for the torn meter rows in a `status` dict, '' when there are none."""
    torn = s.get("torn") or 0
    if not torn:
        return ""
    basis = s.get("cap_basis_usd")
    how = (f"cap basis {basis} USD with each at the month's largest call; a request charges each the larger of that "
           "and its own estimate (an estimate, not a ceiling)" if basis is not None else
           "cap basis unknown: no whole call above 0 tokens this month to charge them at, so each request charges "
           "its own estimate")
    return f"; {torn} torn meter row(s) of unknown size, {how}"
