"""Business headlines from Google News RSS: free, no key, near real time.

Headlines are shown for context only. They never feed the research numbers,
the claims check or the bull/bear case, which use the company's own filings.
"""
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus

from .sources import _cached_get, _ttl

# Words that make a company name ambiguous in a search ("Titan" the moon, "Eternal" the adjective)
QUERY_HINT = {"TITAN": "Titan Company", "ETERNAL": "Eternal Zomato", "HAL": "Hindustan Aeronautics",
              "DMART": "Avenue Supermarts DMart", "CDSL": "CDSL depository", "LT": "Larsen Toubro",
              "ITC": "ITC Ltd", "KPITTECH": "KPIT Technologies", "INFY": "Infosys", "TCS": "TCS Tata Consultancy"}

# A headline must name the company itself, not a namesake or a group cousin
MATCH = {"RELIANCE": r"Reliance Industries|\bRIL\b|Jio|Reliance Retail|Mukesh Ambani", "HDFCBANK": r"HDFC Bank",
         "TCS": r"\bTCS\b|Tata Consultancy", "INFY": r"Infosys", "ITC": r"\bITC\b", "LT": r"L&T|Larsen",
         "BHARTIARTL": r"Airtel", "SUNPHARMA": r"Sun Pharma", "MARUTI": r"Maruti", "TITAN": r"Titan Company|\bTitan\b.*(share|stock|jewel|Tanishq)|Tanishq",
         "BAJFINANCE": r"Bajaj Finance\b", "DMART": r"DMart|D-Mart|Avenue Supermarts", "POLYCAB": r"Polycab",
         "DIXON": r"Dixon", "HAL": r"\bHAL\b|Hindustan Aeronautics", "ETERNAL": r"Eternal|Zomato|Blinkit",
         "CDSL": r"\bCDSL\b|Central Depository", "KPITTECH": r"\bKPIT\b"}
NOT_IT = re.compile(r"Reliance (Power|Infra|Capital|Home|Communications)|Infosys Prize|Infosys Science Foundation|Genex Infosys|Severn Trent|LSE:", re.I)

MARKET_RX = re.compile(r"\b(shares?|stock|NSE|BSE|Q[1-4]|results?|profit|revenue|orders?|dividend|IPO|earnings|"
                       r"crore|Rs|margin|Ltd|Limited|Industries|CEO|CFO|board|acquir\w*|stake)\b|₹", re.I)

NOISE = re.compile(r"\b(stocks? to (buy|watch)|top (gainers|losers)|share price today|^\w+ share price$|stock price, news|"
                   r"buy or sell|target price|stock recommendations?|multibagger|penny stock|stocks in india)\b", re.I)


def _query(symbol, name):
    q = QUERY_HINT.get(symbol) or re.sub(r"\b(Ltd|Limited|Industries|Company|Corporation)\b\.?", "", name or symbol).strip()
    return f'"{q}"' if " " not in q or symbol not in QUERY_HINT else q


OK_NEXT = {"ltd", "limited", "share", "shares", "stock", "nse", "bse", "industries", "q1", "q2", "q3", "q4", "results"}


def _standalone(match, title):
    """'Trent shares rise' is the company; 'Gary Trent Jr' and 'Trent Williams' are people."""
    for m in re.finditer(match, title, re.I):
        before = title[: m.start()].split()[-1:] or [""]
        after = title[m.end():].split()[:1] or [""]
        prev_name = before[0][:1].isupper() and before[0].isalpha()
        next_name = after[0][:1].isupper() and after[0].isalpha() and after[0].lower() not in OK_NEXT
        if not prev_name and not next_name:
            return True
    return False


def headlines(symbol, name, days=7, limit=6):
    q = _query(symbol, name)
    if symbol not in MATCH:  # a company outside the portfolio: steer the search to markets coverage
        q += " (shares OR stock OR NSE OR results)"
    q += f" when:{days}d"
    url = f"https://news.google.com/rss/search?q={quote_plus(q)}&hl=en-IN&gl=IN&ceid=IN:en"
    raw = _cached_get(url, ttl_hours=_ttl(1) if _ttl(1) < 1 else 0.25)
    if not raw:
        return []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return []
    out, seen = [], set()
    match = MATCH.get(symbol) or r"(?<![-\w])" + re.escape(_query(symbol, name).strip('"')) + r"\b"
    cutoff = datetime.utcnow() - timedelta(days=days)
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        src = (it.findtext("source") or "").strip()
        if src and title.endswith(" - " + src):
            title = title[: -len(src) - 3].strip()
        if not title or NOISE.search(title) or NOT_IT.search(title) or not re.search(match, title, re.I):
            continue
        if symbol not in MATCH and not (MARKET_RX.search(title) and _standalone(match, title)):
            continue
        key = re.sub(r"\W+", "", title.lower())[:50]
        if key in seen:
            continue
        seen.add(key)
        try:
            dt = parsedate_to_datetime(it.findtext("pubDate")).replace(tzinfo=None)
        except Exception:
            dt = None
        if dt and dt < cutoff:
            continue
        out.append({"symbol": symbol, "title": title, "source": src, "link": it.findtext("link"),
                    "time": (dt + timedelta(hours=5, minutes=30)).isoformat(timespec="minutes") + "+05:30" if dt else None})
    out.sort(key=lambda x: x["time"] or "", reverse=True)
    return out[:limit]


def portfolio_headlines(portfolio, per_company=3, days=3):
    allh = []
    for p in portfolio:
        allh += headlines(p["symbol"], p["name"], days=days, limit=per_company)
    allh.sort(key=lambda x: x["time"] or "", reverse=True)
    return allh
