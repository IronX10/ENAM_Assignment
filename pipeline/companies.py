"""Every company listed on NSE, ticker and name, for the search menu (data/companies.json).

The app filters this list on the phone as you type, so suggestions appear with no request to a server.
NSE publishes the list as a CSV; the daily refresh rewrites the file, and a failed fetch keeps the last one.
"""
import csv
import io
import re

from .sources import _cached_get

URL = "https://archives.nseindia.com/content/equities/EQUITY_L.csv"
# Index membership ranks the menu, so "tata" shows Tata Motors and TCS before the smaller Tatas.
INDICES = [(3, "ind_nifty50list"), (2, "ind_nifty100list"), (1, "ind_nifty500list")]
INDEX_URL = "https://archives.nseindia.com/content/indices/{}.csv"
ALSO_KNOWN_AS = {"ETERNAL": "Zomato"}  # renamed recently; people still search for the old name


def _rows(raw):
    for r in csv.DictReader(io.StringIO((raw or "").lstrip("\ufeff"))):
        yield {k.strip(): (v or "").strip() for k, v in r.items() if k}


def ranks():
    out = {}
    for rank, name in INDICES:
        for r in _rows(_cached_get(INDEX_URL.format(name), ttl_hours=20)):
            if r.get("Symbol"):
                out.setdefault(r["Symbol"], rank)
    return out


def short_name(name):
    """'Tata Consultancy Services Limited' -> 'Tata Consultancy Services'."""
    n = " ".join((name or "").split())
    n = re.sub(r"\s*\((?:the|formerly[^)]*)\)\s*$", "", n, flags=re.I)
    n = re.sub(r"[\s,.]*\b(limited|ltd\.?)$", "", n, flags=re.I)
    n = n.strip(" ,.") or name
    return n.title() if n.isupper() and len(n) > 5 else n  # 'ETERNAL' -> 'Eternal'


def companies():
    """[[ticker, name, rank]], rank 3 for the Nifty 50 down to 0 for a company in no Nifty 500 index."""
    raw = _cached_get(URL, ttl_hours=20)
    if not raw:
        return None
    rank = ranks()
    rows = []
    for r in _rows(raw):
        sym, name = r.get("SYMBOL"), r.get("NAME OF COMPANY")
        if sym and name:
            name = short_name(name)
            if sym in ALSO_KNOWN_AS:
                name += f" ({ALSO_KNOWN_AS[sym]})"
            rows.append([sym, name, rank.get(sym, 0)])
    # a short or empty answer means NSE sent an error page, not the list
    return sorted(rows) if len(rows) > 1000 else None
