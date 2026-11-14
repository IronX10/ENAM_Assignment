"""Plumbing for the three data sources: NSE filings, Yahoo EOD prices, screener.in.

All fetches go through one cached session so re-runs are free and a blocked
source never breaks the app (the app only reads the JSON this writes).
"""
import hashlib
import json
import os
import re
import time
from datetime import datetime, timedelta

import requests
from bs4 import BeautifulSoup

# Live mode (the server) refreshes filings and prices every few minutes; batch mode caches longer.
LIVE_MIN = float(os.environ.get("FINN_LIVE_TTL_MIN", "0") or 0)


def _ttl(batch_hours):
    return LIVE_MIN / 60 if LIVE_MIN else batch_hours


def ist_now():
    """Exchange time. Servers usually run in UTC; NSE timestamps are IST."""
    return datetime.utcnow() + timedelta(hours=5, minutes=30)


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(ROOT, "data", "cache")
os.makedirs(CACHE, exist_ok=True)

_S = requests.Session()
_S.headers.update({
    "User-Agent": "Mozilla/5.0 (FINN prototype; research use)",
    "Accept-Language": "en-US,en;q=0.9",
})


def _cached_get(url, ttl_hours=6, binary=False):
    key = hashlib.sha1(url.encode()).hexdigest()[:16]
    path = os.path.join(CACHE, key + (".bin" if binary else ".txt"))
    if os.path.exists(path) and (time.time() - os.path.getmtime(path)) < ttl_hours * 3600:
        with open(path, "rb" if binary else "r", encoding=None if binary else "utf-8") as f:
            return f.read()
    for attempt in range(3):
        try:
            r = _S.get(url, timeout=40)
            if r.status_code == 200:
                data = r.content if binary else r.text
                with open(path, "wb" if binary else "w", encoding=None if binary else "utf-8") as f:
                    f.write(data)
                return data
            if r.status_code in (404, 410):
                return None  # a wrong ticker or a removed file: retrying will not help
            if r.status_code in (401, 403) and "nseindia" in url:
                _S.get("https://www.nseindia.com/", timeout=20)  # refresh cookies
        except requests.RequestException:
            pass
        time.sleep(1.5 * (attempt + 1))
    return None


# ---------------------------------------------------------------- NSE filings

def nse_announcements(symbol, from_date, to_date):
    """from/to as datetime. Returns normalised filing dicts."""
    url = ("https://www.nseindia.com/api/corporate-announcements?index=equities"
           f"&symbol={symbol}&from_date={from_date:%d-%m-%Y}&to_date={to_date:%d-%m-%Y}")
    raw = _cached_get(url, ttl_hours=_ttl(1))
    if not raw:
        return []
    try:
        rows = json.loads(raw)
    except ValueError:
        return []
    out = []
    for x in rows:
        t = datetime.strptime(x["an_dt"], "%d-%b-%Y %H:%M:%S")
        out.append({
            "id": f"nse-{x.get('seq_id') or hashlib.sha1((symbol + x['an_dt']).encode()).hexdigest()[:10]}",
            "symbol": symbol,
            "company": x.get("sm_name", symbol),
            "time": t.isoformat(),
            "subject": (x.get("desc") or "").strip(),
            "text": clean_exchange_text(x.get("attchmntText") or "", x.get("sm_name", "")),
            "summary": (x.get("voiceSummary") or "").strip(),
            "pdf": x.get("attchmntFile"),
            "industry": x.get("smIndustry"),
        })
    return out


def clean_exchange_text(text, company):
    """'X Limited has informed the Exchange about Y' -> 'Y'."""
    t = " ".join(text.split())
    t = re.sub(re.escape(company) + r"\s+has\s+(informed|submitted to)\s+the\s+Exchange\s+(about|regarding|that)?\s*", "", t, flags=re.I)
    t = re.sub(r"^(the\s+)?company\s+has\s+informed\s+the\s+exchange\s+(about|regarding|that)\s*", "", t, flags=re.I)
    return t[:1].upper() + t[1:] if t else t


_PDF_MEMO = {}


def pdf_pages(url, max_pages=6):
    """Text of each of the first pages of a PDF. Filings and transcripts are PDFs.
    Parsing is the slowest step on a small server, so the extracted text is cached on disk and in
    memory: each PDF is parsed once, not on every 2-minute refresh."""
    if not url:
        return []
    key = hashlib.sha1(url.encode()).hexdigest()[:16]
    tpath = os.path.join(CACHE, key + ".pages.json")
    have = _PDF_MEMO.get(key)
    if have is None and os.path.exists(tpath):
        try:
            with open(tpath, encoding="utf-8") as f:
                have = json.load(f)
        except (OSError, ValueError):
            have = None
    if have is not None and (have["n"] >= max_pages or have["complete"]):
        _PDF_MEMO[key] = have
        return have["pages"][:max_pages]
    data = _cached_get(url, ttl_hours=24 * 30, binary=True)
    if not data or not data[:4] == b"%PDF":
        return []
    try:
        import io
        from pypdf import PdfReader
        r = PdfReader(io.BytesIO(data))
        pages = [(p.extract_text() or "") for p in r.pages[:max_pages]]
        have = {"n": max_pages, "complete": len(r.pages) <= max_pages, "pages": pages}
    except Exception:
        return []
    _PDF_MEMO[key] = have
    if len(_PDF_MEMO) > 400:
        _PDF_MEMO.pop(next(iter(_PDF_MEMO)))
    try:
        with open(tpath, "w", encoding="utf-8") as f:
            json.dump(have, f)
    except OSError:
        pass
    return pages


def pdf_text(url, max_pages=6):
    return "\n".join(pdf_pages(url, max_pages))


# ---------------------------------------------------------------- Yahoo EOD

def eod_prices(symbol, rng="6mo"):
    ysym = symbol if symbol.startswith("^") else symbol + ".NS"
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ysym}?range={rng}&interval=1d"
    raw = _cached_get(url, ttl_hours=_ttl(3))
    if not raw:
        return None
    try:
        res = json.loads(raw)["chart"]["result"][0]
        q = res["indicators"]["quote"][0]
        rows = []
        for ts, c, v in zip(res["timestamp"], q["close"], q["volume"]):
            if c is None:
                continue
            rows.append({"date": datetime.utcfromtimestamp(ts + 19800).strftime("%Y-%m-%d"),
                         "close": round(c, 2), "volume": int(v or 0)})
        return rows
    except (KeyError, IndexError, ValueError, TypeError):
        return None


def live_quote(symbol):
    """Latest traded price and change vs previous close (Yahoo, refreshed every minute by the server)."""
    ysym = symbol if symbol.startswith("^") else symbol + ".NS"
    try:
        r = _S.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{ysym}?range=1d&interval=5m", timeout=15)
        m = r.json()["chart"]["result"][0]["meta"]
        price, prev = m.get("regularMarketPrice"), m.get("chartPreviousClose") or m.get("previousClose")
        return {"price": price, "prev": prev, "chg": round((price / prev - 1) * 100, 2) if price and prev else None,
                "time": datetime.utcfromtimestamp(m["regularMarketTime"] + 19800).isoformat(timespec="seconds") + "+05:30",
                "volume": m.get("regularMarketVolume")}
    except Exception:
        return None


def live_quotes(symbols):
    """Latest price, change and today's minute-by-minute line for many symbols in ONE request
    (Yahoo spark), so the server can afford to refresh every 30 seconds."""
    ys = {(x if x.startswith("^") else x + ".NS"): x for x in symbols}
    out = {}
    keys = list(ys)
    for i in range(0, len(keys), 20):  # the endpoint takes up to 20 symbols per call
        try:
            r = _S.get("https://query1.finance.yahoo.com/v8/finance/spark",
                       params={"symbols": ",".join(keys[i:i + 20]), "range": "1d", "interval": "1m"}, timeout=15)
            data = r.json()
        except Exception:
            continue
        for ysym, v in (data or {}).items():
            sym = ys.get(ysym)
            pts = [(t, c) for t, c in zip(v.get("timestamp") or [], v.get("close") or []) if c is not None]
            prev = v.get("chartPreviousClose") or v.get("previousClose")
            if not sym or not pts:
                continue
            price = round(pts[-1][1], 2)
            out[sym] = {
                "price": price, "prev": prev,
                "chg": round((price / prev - 1) * 100, 2) if prev else None,
                "time": datetime.utcfromtimestamp(pts[-1][0] + 19800).isoformat(timespec="seconds") + "+05:30",
                # every 5th minute keeps the payload small: ~75 points for a full session
                "day": [round(c, 2) for _, c in pts[::5]] + ([price] if len(pts) % 5 != 1 else []),
            }
    missing = [x for x in symbols if x not in out]
    for x in missing:  # fall back to one call per symbol if the batch failed
        q = live_quote(x)
        if q:
            out[x] = q
    return out


# ---------------------------------------------------------------- screener.in

def _num(s):
    s = (s or "").replace(",", "").replace("%", "").replace("+", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def screener(symbol, variants=("consolidated/", "")):
    """Standardised tables (from annual reports / quarterly filings) + document links.
    Consolidated first; pass variants=("",) for the standalone statements."""
    html = None
    for variant in variants:
        html = _cached_get(f"https://www.screener.in/company/{symbol}/{variant}", ttl_hours=12)
        if html and 'id="quarters"' in html:
            # consolidated pages for companies without subsidiaries can be empty; check data rows
            soup = BeautifulSoup(html, "lxml")
            q = soup.find("section", id="quarters")
            if q and q.find("tbody") and len(q.find("tbody").find_all("tr")) > 3:
                break
    if not html:
        return None
    soup = BeautifulSoup(html, "lxml")
    out = {"symbol": symbol, "url": f"https://www.screener.in/company/{symbol}/{variant}",
           "basis": "consolidated" if variant else "standalone"}
    h1 = soup.find("h1")
    out["name"] = h1.get_text(strip=True) if h1 else symbol

    def table(sec_id):
        sec = soup.find("section", id=sec_id)
        if not sec or not sec.find("table"):
            return None
        t = sec.find("table")
        cols = [th.get_text(strip=True) for th in t.find("thead").find_all("th")][1:]
        rows = {}
        for tr in t.find("tbody").find_all("tr"):
            tds = tr.find_all("td")
            if not tds:
                continue
            label = tds[0].get_text(" ", strip=True).replace("+", "").strip()
            label = re.split(r"(?<=[a-z%])(?=[A-Z][a-z]+ of )", label)[0]  # drop tooltip text
            rows[label] = [_num(td.get_text(strip=True)) for td in tds[1:]]
        return {"cols": cols, "rows": rows}

    for key, sec in [("quarters", "quarters"), ("pl", "profit-loss"), ("bs", "balance-sheet"),
                     ("cf", "cash-flow"), ("ratios", "ratios"), ("holding", "shareholding")]:
        out[key] = table(sec)

    top = {}
    ul = soup.find("ul", id="top-ratios")
    if ul:
        for li in ul.find_all("li"):
            name = li.find("span", class_="name")
            val = li.find("span", class_="value")
            if name and val:
                top[name.get_text(strip=True)] = " ".join(val.get_text(" ", strip=True).split())
    out["top"] = top
    about = soup.find("div", class_="about")
    out["about"] = " ".join(about.get_text(" ", strip=True).split()) if about else ""
    out["about"] = re.sub(r"\s*\[\d+\]", "", out["about"])
    com = soup.find("div", class_="commentary")
    out["segments_note"] = com.get_text("\n", strip=True) if com else ""
    out["segments_src"] = None
    if com and com.find("a"):
        out["segments_src"] = com.find("a").get("href")

    docs = {"annual_reports": [], "concalls": []}
    sec = soup.find("section", id="documents")
    if sec:
        for div in sec.find_all("div", class_="documents"):
            h = div.find("h3")
            title = h.get_text(strip=True) if h else ""
            for li in div.find_all("li"):
                links = li.find_all("a")
                if title.startswith("Annual"):
                    if links:
                        docs["annual_reports"].append({"label": " ".join(links[0].get_text(" ", strip=True).split()),
                                                       "url": links[0].get("href")})
                elif title.startswith("Concall"):
                    period = li.find("div")
                    period = period.get_text(strip=True) if period else ""
                    tr = next((a.get("href") for a in links if a.get_text(strip=True) == "Transcript"), None)
                    ppt = next((a.get("href") for a in links if a.get_text(strip=True) == "PPT"), None)
                    docs["concalls"].append({"period": period, "transcript": tr, "ppt": ppt})
    out["docs"] = docs
    return out


def series(tbl, *labels):
    """First matching row from a screener table."""
    if not tbl:
        return None
    for lab in labels:
        for k, v in tbl["rows"].items():
            if k.lower().startswith(lab.lower()):
                return v
    return None
