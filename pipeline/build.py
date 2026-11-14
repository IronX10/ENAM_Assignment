"""Run the whole pipeline and write the JSON the app reads.

    python -m pipeline.build              # push feed + research for portfolio and demo set
    python -m pipeline.build --days 30    # look-back window for filings
    python -m pipeline.build --research TRENT   # one ticker only

The app never calls a source directly. It reads data/*.json, so a slow or
blocked source can never break the demo, and the whole thing can be hosted as
static files refreshed by a scheduled job.
"""
import argparse
import json
import os
import time
from datetime import datetime, timedelta

from . import llm, news, push, research
from .config import DEFAULT_RULES, INDEX_SYMBOL, PORTFOLIO, RESEARCH_EXTRA
from .sources import ROOT, eod_prices, ist_now, nse_announcements, screener

DATA = os.path.join(ROOT, "data")


def write(name, obj):
    path = os.path.join(DATA, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    return path


def build_push(days):
    now = ist_now()
    start = now - timedelta(days=days)
    weights = {p["symbol"]: p["weight"] for p in PORTFOLIO}
    names = {p["symbol"]: p["name"] for p in PORTFOLIO}

    filings, screeners, prices = [], {}, {}
    for p in PORTFOLIO:
        s = p["symbol"]
        filings += nse_announcements(s, start, now)
        screeners[s] = screener(s)
        prices[s] = eod_prices(s)
        time.sleep(0.15)
    index_rows = eod_prices(INDEX_SYMBOL)

    events = push.build_events(filings, screeners, prices, index_rows, weights)
    for e in events:
        e["name"] = names.get(e["symbol"], e["company"])
        e.pop("industry", None)
        e.pop("_gist", None)
    pxe = push.price_events(prices, index_rows, weights, names, since=start.strftime("%Y-%m-%d"))

    # compact price history for sparklines and for the app's own threshold checks
    spark = {}
    for s, rows in prices.items():
        if rows:
            spark[s] = {"d": [r["date"] for r in rows[-60:]], "c": [r["close"] for r in rows[-60:]],
                        "v": [r["volume"] for r in rows[-60:]]}
    if index_rows:
        spark["NIFTY"] = {"d": [r["date"] for r in index_rows[-60:]], "c": [r["close"] for r in index_rows[-60:]], "v": []}

    feed = {
        "generated": now.isoformat(timespec="minutes"),
        "window_days": days,
        "portfolio": PORTFOLIO,
        "rules": DEFAULT_RULES,
        "events": events,
        "price_moves": pxe,
        "prices": spark,
        "news": _headlines(),
        "stats": {"filings": len(filings), "cards": len(events)},
    }
    return write("feed.json", feed), feed


def _headlines():
    """Business headlines for the holdings (Google News, free). Context only: never scored or analysed."""
    try:
        return news.portfolio_headlines(PORTFOLIO, per_company=3, days=3)
    except Exception as ex:
        print("news failed:", ex)
        return []


def build_research(symbols):
    index = []
    for s in symbols:
        try:
            r = research.build(s)
        except Exception as ex:  # one bad ticker must not stop the run
            r = {"symbol": s, "error": f"Could not build: {ex}"}
        write(f"research/{s}.json", r)
        if not r.get("error"):
            index.append({"symbol": s, "name": r["name"], "credibility": r["verdict"]["credibility"]})
        print("research", s, "ok" if not r.get("error") else r["error"])
    return index


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--research", nargs="*")
    ap.add_argument("--skip-push", action="store_true")
    a = ap.parse_args()

    if not a.skip_push and not a.research:
        path, feed = build_push(a.days)
        print("feed", path, feed["stats"])
    syms = a.research or [p["symbol"] for p in PORTFOLIO] + RESEARCH_EXTRA
    idx = build_research(syms)
    if not a.research:
        write("research/index.json", idx)
    print("model usage", llm.LEDGER)


if __name__ == "__main__":
    main()
