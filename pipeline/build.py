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
import threading
import time
from datetime import datetime, timedelta

from . import companies, llm, news, push, research
from .config import DEFAULT_RULES, INDEX_SYMBOL, PORTFOLIO, RESEARCH_EXTRA
from .sources import ROOT, eod_prices, ist_now, last_fetch_failed, nse_announcements, screener

DATA = os.path.join(ROOT, "data")


def write(name, obj):
    path = os.path.join(DATA, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"  # write then rename: the app never reads half a file
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)
    return path


def read(name):
    try:
        with open(os.path.join(DATA, name), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


YIELD = lambda: None  # the live server swaps this in so a research request someone is waiting on goes first
_LAST = {"filings_checked": None}  # when NSE last answered; carried forward while it cannot be reached


def seed(feed):
    """Tell the builder what the saved feed already knew, so a restart does not forget it."""
    if feed:
        _LAST["filings_checked"] = feed.get("filings_checked") or feed.get("generated")


def build_push(days):
    now = ist_now()
    start = now - timedelta(days=days)
    weights = {p["symbol"]: p["weight"] for p in PORTFOLIO}
    names = {p["symbol"]: p["name"] for p in PORTFOLIO}

    filings, screeners, prices, answered, silent = [], {}, {}, 0, []
    for p in PORTFOLIO:
        YIELD()
        s = p["symbol"]
        got = nse_announcements(s, start, now)
        answered += 0 if last_fetch_failed() else 1
        if not got:
            silent.append(s)
        filings += got
        screeners[s] = screener(s)
        prices[s] = eod_prices(s)
        time.sleep(0.15)  # be polite to the sources
    # Every holding files something in a month. If a third of them come back empty, NSE is blocking or
    # down, and a feed built from that would silently drop real filings: keep the last good one instead.
    if len(silent) * 3 > len(PORTFOLIO):
        raise RuntimeError(f"NSE gave no filings for {len(silent)} of {len(PORTFOLIO)} holdings; kept the last good feed")
    stamp = now.isoformat(timespec="minutes")
    if answered * 2 >= len(PORTFOLIO):
        _LAST["filings_checked"] = stamp
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
        "generated": stamp,
        "filings_checked": _LAST["filings_checked"] or stamp,  # older than "generated" while NSE cannot be reached
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
    """Research files for these tickers. A ticker that fails keeps its saved file and its place in the
    index, so a blocked source can never replace good research with an error."""
    old = {x["symbol"]: x for x in (read("research/index.json") or []) if isinstance(x, dict)}
    index = []
    for s in symbols:
        try:
            r = research.build(s)
        except Exception as ex:  # one bad ticker must not stop the run
            r = {"symbol": s, "error": f"Could not build: {ex}"}
        if r.get("error"):
            print("research", s, r["error"], "(kept the saved copy)" if s in old else "")
            if s in old:
                index.append(old[s])
            continue
        write(f"research/{s}.json", r)
        index.append({"symbol": s, "name": r["name"], "credibility": r["verdict"]["credibility"]})
        print("research", s, "ok")
    return index


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--research", nargs="*")
    ap.add_argument("--skip-push", action="store_true")
    a = ap.parse_args()

    if not a.skip_push and not a.research:
        seed(read("feed.json"))
        try:
            path, feed = build_push(a.days)
            print("feed", path, feed["stats"])
        except Exception as ex:  # a blocked source must not wipe the saved snapshot
            print("feed not refreshed:", ex)
    if not a.research:
        try:
            listed = companies.companies()  # for the search menu; kept as it was if NSE does not answer
        except Exception as ex:
            listed = None
            print("company list not refreshed:", ex)
        if listed:
            write("companies.json", listed)
            print("companies", len(listed))
    syms = a.research or [p["symbol"] for p in PORTFOLIO] + RESEARCH_EXTRA
    idx = build_research(syms)
    if not a.research and idx:
        write("research/index.json", idx)
    print("model usage", llm.LEDGER)


if __name__ == "__main__":
    main()
