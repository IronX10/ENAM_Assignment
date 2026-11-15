"""Optional model calls, routed by cost.

Delta runs without any key: rules and code produce every number, and NSE's own
filing summaries fill the card text. With a key, two jobs use a model:

  cheap model  -> compress a bio, extract guidance rows from transcript snippets
  strong model -> write promised-vs-delivered verdicts (one call per company)

Results are cached on disk by input hash, so a company costs nothing after its
first run in a quarter.
"""
import hashlib
import json
import os

import requests

CHEAP = os.environ.get("FINN_CHEAP_MODEL", "claude-haiku-5-5")
STRONG = os.environ.get("FINN_STRONG_MODEL", "claude-sonnet-5-5")
KEY = os.environ.get("ANTHROPIC_API_KEY")
_CACHE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "cache", "llm")
os.makedirs(_CACHE, exist_ok=True)

LEDGER = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cache_hits": 0}


def _call(model, system, user, max_tokens=800):
    key = hashlib.sha1((model + system + user).encode()).hexdigest()
    path = os.path.join(_CACHE, key + ".json")
    if os.path.exists(path):
        LEDGER["cache_hits"] += 1
        return json.load(open(path))["text"]
    if not KEY:
        return None
    r = requests.post("https://api.anthropic.com/v1/messages", timeout=120, headers={
        "x-api-key": KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        json={"model": model, "max_tokens": max_tokens, "system": system,
              "messages": [{"role": "user", "content": user}]})
    if r.status_code != 200:
        return None
    body = r.json()
    text = "".join(b.get("text", "") for b in body.get("content", []))
    u = body.get("usage", {})
    LEDGER["calls"] += 1
    LEDGER["input_tokens"] += u.get("input_tokens", 0)
    LEDGER["output_tokens"] += u.get("output_tokens", 0)
    json.dump({"text": text}, open(path, "w"))
    return text


def maybe_compress(text, instruction):
    if not text or not KEY:
        return None
    return _call(CHEAP, "You compress text. Use only facts in the input. No preamble.",
                 f"{instruction}\n\n<input>{text}</input>", 200)


GUIDANCE_SYSTEM = """You read earnings-call transcript snippets and extract explicit, forward-looking
management guidance on headline numbers only (revenue/AUM/volume growth, margins, capex, store or
capacity additions, credit cost, debt). Ignore vague optimism. Use only the snippets; never outside
knowledge. Output JSON: {"items":[{"said_in":"<quarter label>","metric":"...","promise":"<short>",
"target_value":<number or null>,"unit":"%|cr|count","horizon":"<FY or period>","quote":"<exact words>",
"page":<int or null>}]}"""


def extract_guidance(period, snippets):
    if not snippets:
        return []
    user = f"Call: {period}\n\n" + "\n\n".join(f"[p{p}] {s}" for p, s in snippets)
    out = _call(CHEAP, GUIDANCE_SYSTEM, user, 1200)
    if not out:
        return []
    try:
        start = out.index("{")
        return json.loads(out[start:out.rindex("}") + 1]).get("items", [])
    except ValueError:
        return []


VERDICT_SYSTEM = """You compare what management promised with what the reported numbers show.
For each promise, given the actual figures computed from filings, return a verdict:
kept | missed | partly | dropped | pending, and one sentence (<=22 words) saying why, citing the numbers.
Use only the inputs. Output JSON {"items":[{"i":<index>,"verdict":"...","why":"..."}]}"""


def verdicts(promises, actuals):
    out = _call(STRONG, VERDICT_SYSTEM, json.dumps({"promises": promises, "actuals": actuals}), 1500)
    if not out:
        return None
    try:
        return json.loads(out[out.index("{"):out.rindex("}") + 1]).get("items", [])
    except ValueError:
        return None
