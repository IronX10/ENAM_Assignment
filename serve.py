"""FINN live server.

    python serve.py            # http://localhost:8000

Open the address on any phone or computer. The server keeps the data fresh on
its own, so the app always opens instantly with recent data:

  prices      every 15 seconds (all holdings + Nifty in one Yahoo request, with today's intraday line)
  filings     every 2 minutes  (NSE announcements, re-scored and re-ranked) with headlines (Google News)
  research    on request       (any NSE ticker; financials cached 12 hours)

The app asks /api/live every 30 seconds: one small reply with the prices and the
version of the feed, so it only downloads the feed again when something changed.

Until the first live refresh finishes after a cold start, the app is served the
last saved snapshot (data/feed.json) and says so.

Routes: /api/live[?watch=X]  /api/feed  /api/quotes  /api/research?symbol=X  /api/status
"""
import json
import os
import re
import threading
import time
import traceback
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

os.environ.setdefault("FINN_LIVE_TTL_MIN", "1.5")  # must be set before the pipeline is imported

from pipeline import build, research  # noqa: E402
from pipeline.config import INDEX_SYMBOL, PORTFOLIO  # noqa: E402
from pipeline.sources import ist_now, live_quotes  # noqa: E402

ROOT = os.path.dirname(os.path.abspath(__file__))
FEED_EVERY = int(os.environ.get("FINN_FEED_EVERY_SEC", 120))
QUOTES_EVERY = int(os.environ.get("FINN_QUOTES_EVERY_SEC", 15))  # app polls every 30 s, so shown prices are under 45 s old
DAYS = int(os.environ.get("FINN_DAYS", 30))

STATE = {"feed": None, "feed_live": False, "feed_error": None, "quotes": {}, "quotes_at": None, "checked_at": None,
         "extra": {}}  # research tickers someone is looking at, priced on the same 30-second beat
LOCK = threading.Lock()


def load_snapshot():
    path = os.path.join(ROOT, "data", "feed.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            STATE["feed"] = json.load(f)


def market_open():
    t = ist_now()
    return t.weekday() < 5 and (9, 0) <= (t.hour, t.minute) <= (15, 45)


def feed_loop():
    while True:
        try:
            _, feed = build.build_push(DAYS)
            with LOCK:
                STATE["feed"], STATE["feed_live"], STATE["feed_error"] = feed, True, None
            print(f"[feed] {feed['generated']} {feed['stats']}", flush=True)
        except Exception as ex:
            STATE["feed_error"] = str(ex)
            traceback.print_exc()
        t = ist_now()
        busy = t.weekday() < 5 and (market_open() or t.hour in range(15, 23))  # results land in the evening too
        time.sleep(FEED_EVERY if busy else FEED_EVERY * 5)


def quotes_loop():
    while True:
        now = time.time()
        with LOCK:
            extra = [s for s, seen in STATE["extra"].items() if now - seen < 600]
            STATE["extra"] = {s: STATE["extra"][s] for s in extra}
        syms = [p["symbol"] for p in PORTFOLIO] + [INDEX_SYMBOL] + extra
        out = live_quotes(syms)
        if INDEX_SYMBOL in out:
            out["NIFTY"] = out.pop(INDEX_SYMBOL)
        stamp = ist_now().isoformat(timespec="seconds") + "+05:30"
        with LOCK:
            if out:
                STATE["quotes"], STATE["quotes_at"] = out, max((q.get("time") or "") for q in out.values()) or stamp
            STATE["checked_at"] = stamp
        # prices only move in market hours; outside them a check every 5 minutes is plenty
        time.sleep(QUOTES_EVERY if market_open() else 300)


def live_payload():
    f = STATE["feed"] or {}
    return {"checked_at": STATE["checked_at"], "quotes_at": STATE["quotes_at"], "market_open": market_open(),
            "quotes": STATE["quotes"], "feed_generated": f.get("generated"), "live": STATE["feed_live"],
            "every": {"prices": QUOTES_EVERY if market_open() else 300, "filings": FEED_EVERY}}


class Handler(SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def translate_path(self, path):
        p = urlparse(path).path
        if p.startswith("/data/"):
            return os.path.join(ROOT, p.lstrip("/"))
        return os.path.join(ROOT, "app", p.lstrip("/") or "index.html")

    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/api/feed":
            with LOCK:
                feed = dict(STATE["feed"] or {}, live=STATE["feed_live"], market_open=market_open())
            return self._json(feed)
        if u.path == "/api/live":
            sym = re.sub(r"[^A-Z0-9&-]", "", (parse_qs(u.query).get("watch", [""])[0]).upper())
            with LOCK:
                if sym and sym not in STATE["quotes"]:
                    STATE["extra"][sym] = time.time()  # priced from the next 30-second beat
                elif sym in STATE["extra"]:
                    STATE["extra"][sym] = time.time()
                return self._json(live_payload())
        if u.path == "/api/quotes":
            with LOCK:
                return self._json({"at": STATE["quotes_at"], "market_open": market_open(), "quotes": STATE["quotes"]})
        if u.path == "/api/status":
            return self._json({"live": STATE["feed_live"], "generated": (STATE["feed"] or {}).get("generated"),
                               "quotes_at": STATE["quotes_at"], "checked_at": STATE["checked_at"],
                               "error": STATE["feed_error"], "market_open": market_open()})
        if u.path == "/api/research":
            sym = re.sub(r"[^A-Z0-9&-]", "", (parse_qs(u.query).get("symbol", [""])[0]).upper())
            if not sym:
                return self._json({"error": "Type an NSE ticker, for example TRENT."}, 400)
            try:
                r = research.build(sym)  # screener.in pages are cached for 12 hours
            except Exception as ex:
                r = {"symbol": sym, "error": f"Could not build research for {sym}: {ex}"}
            if not r.get("error"):
                build.write(f"research/{sym}.json", r)
            return self._json(r)
        return super().do_GET()


if __name__ == "__main__":
    load_snapshot()
    threading.Thread(target=quotes_loop, daemon=True).start()
    threading.Thread(target=feed_loop, daemon=True).start()
    port = int(os.environ.get("PORT", 8000))
    print(f"FINN live on http://localhost:{port}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
