"""Push side: filings + EOD prices -> classified, enriched, scored events.

Rules decide; nothing here needs a model. NSE now ships its own summary of each
filing (voiceSummary), so most cards reuse that text for free. A model is only
called (optionally, see llm.py) for things rules cannot read.
"""
import re
from collections import defaultdict
from datetime import datetime, timedelta

from . import llm
from .sources import pdf_text, series

# ------------------------------------------------------------------ classify

RULES = [  # (category, subtype, regex on "subject | text")
    ("results", "results", r"financial result|outcome of board meeting.*result|integrated filing \(financial\)|unaudited.*results|audited.*results"),
    ("results", "deck", r"^investor presentation"),
    ("unclassified", "newspaper", r"newspaper publication"),
    ("unclassified", "esop", r"esop|esos|esps|employee stock|allotment of \d[\d,]* (equity )?shares"),
    ("unclassified", "compliance", r"trading window|certificate under|reg(ulation)?\.? ?74|loss of share|duplicate share|statement of deviation|monitoring agency|change in auditors|secretarial|compliance certificate|xbrl"),
    ("management_change", None, r"change in management|change in director|key managerial|retirement|cessation|resignation|appointment|re-appointment"),
    ("M&A", None, r"acquisition|amalgamation|merger|demerger|sale or disposal|scheme of arrangement|joint venture|restructuring|wholly owned subsidiary|stake"),
    ("fund_raise", None, r"buy ?back|qualified institutional|rights issue|preferential|issue of securities|commercial paper|debenture|ncd|fund rais"),
    ("litigation", None, r"action\(s\)|orders? passed|insolvency|litigation|dispute|penalt|show cause|tax demand|regulatory action|fine"),
    ("insider_trading", None, r"insider trading|takeover regulations|sast|regulation 29|regulation 7\(2\)"),
    ("pledging", None, r"pledge|encumbrance"),
    ("credit_rating", None, r"credit rating"),
    ("dividend", None, r"dividend"),
    ("bonus_split", None, r"bonus|sub-division|split"),
    ("capex", None, r"capacity|capex|commencement of commercial production|new plant|expansion"),
    ("earnings_call", None, r"(earnings|conference|con\.?) call|transcript|audio recording"),
    ("investor_meetings", None, r"analysts/institutional investor meet|investor meet|investor conference"),
    ("agm_egm", None, r"shareholders meeting|postal ballot|annual general meeting|\bagm\b|\begm\b|scrutini"),
    ("press_release", "order_win", r"bagging|receiving of orders|order win|contract"),
    ("press_release", "clarification", r"news verification|rumour|clarification"),
    ("press_release", None, r"press release|general updates|^updates|launch|partnership"),
]


def classify(f):
    subj = f["subject"].lower()
    blob = (f["subject"] + " | " + f["text"]).lower()
    # The exchange subject line is the strongest signal; check it first, then the text.
    for scope in (subj, blob):
        for cat, sub, rx in RULES:
            if re.search(rx, scope):
                if cat == "earnings_call" and scope is subj:
                    continue
                return _subtype(cat, sub, f)
    return _subtype("unclassified", "other", f)


ROLE_RX = [
    ("CFO", r"\b(cfo|chief financial)"), ("CEO", r"\b(ceo|chief executive)(?! ?\()"), ("MD", r"managing director|\bmd\b"),
    ("Independent Director", r"independent director"), ("Vice Chairman", r"vice[- ]chair"),
    ("Chairman", r"chair(man|person)"),
    ("Company Secretary", r"company secretary|compliance officer"), ("Director", r"director"),
    ("Senior management", r"senior management|smp|key managerial|president|head"),
]


def _subtype(cat, sub, f):
    blob = (f["subject"] + " " + f["text"] + " " + f["summary"]).lower()
    if cat == "management_change":
        role = next((r for r, rx in ROLE_RX if re.search(rx, blob)), "Senior management")
        if re.search(r"resign", blob):
            act = "resigns"
        elif re.search(r"retire|cessation|completed (his|her|their) .*term|ceases?", blob):
            act = "steps down"
        elif re.search(r"appoint|joins|designat", blob):
            act = "appointed"
        else:
            act = "changes"
        sub = f"{role}|{act}"
    elif cat == "investor_meetings" and re.search(r"(earnings|conference) call|transcript|audio|recording", blob):
        cat, sub = "earnings_call", None
    elif cat == "credit_rating":
        sub = "downgrade" if "downgrad" in blob else "upgrade" if "upgrad" in blob else "reaffirm" if re.search(r"reaffirm|retain", blob) else "action"
    elif cat == "M&A" and re.search(r"strike.?off", blob):
        sub = "strike_off"
    elif cat == "M&A" and re.search(r"incorporation of (a )?wholly owned|incorporated .{0,40}wholly owned", blob):
        sub = "new_subsidiary"
    elif cat == "fund_raise" and "buy" in blob and "back" in blob:
        sub = "buyback"
    return cat, sub


# ------------------------------------------------------------------ enrich

AMT_RX = re.compile(r"(?:₹|rs\.?|inr)\s*([\d,]+(?:\.\d+)?)\s*(crore|cr\b|lakh|million|billion|bn)", re.I)
LT_ORDER_BANDS = {"significant": (1000, 2500), "large": (2500, 5000), "major": (5000, 10000),
                  "mega": (10000, 15000), "ultra-mega": (15000, None)}


def amount_cr(text):
    """Largest rupee amount mentioned, in crore."""
    best = None
    for num, unit in AMT_RX.findall(text or ""):
        v = float(num.replace(",", ""))
        u = unit.lower()
        v = v / 100 if u == "lakh" else v / 10 if u == "million" else v * 100 if u in ("billion", "bn") else v
        best = v if best is None or v > best else best
    return best


def pct(a, b):
    if a is None or b in (None, 0):
        return None
    return round((a - b) / abs(b) * 100, 1)


def fy_quarter(col):
    """'Sep 2026' -> 'Q2 FY27' (Indian fiscal year ends in March)."""
    m = re.match(r"(\w{3}) (\d{4})", col or "")
    if not m:
        return col
    q = {"Jun": 1, "Sep": 2, "Dec": 3, "Mar": 4}.get(m.group(1))
    yr = int(m.group(2)) + (0 if m.group(1) == "Mar" else 1)
    return f"Q{q} FY{str(yr)[2:]}" if q else col


def results_block(scr):
    """Headline numbers QoQ / YoY from the standardised quarterly table."""
    q = scr and scr.get("quarters")
    if not q or len(q["cols"]) < 5:
        return None
    rev = series(q, "Sales", "Revenue")
    ebitda = series(q, "Operating Profit", "Financing Profit")
    pbt = series(q, "Profit before tax")
    pat = series(q, "Net Profit")
    is_bank = series(q, "Financing Profit") is not None and series(q, "Operating Profit") is None
    cols = q["cols"]

    def row(label, s):
        if not s:
            return None
        return {"label": label, "now": s[-1], "prev": s[-2], "yago": s[-5],
                "qoq": pct(s[-1], s[-2]), "yoy": pct(s[-1], s[-5])}

    rows = [r for r in [row("Revenue", rev), row("Pre-provision profit" if is_bank else "EBITDA", ebitda),
                        row("PBT", pbt), row("PAT", pat)] if r]
    margin = None
    if rev and ebitda and rev[-1] and rev[-5]:
        margin = {"now": round(ebitda[-1] / rev[-1] * 100, 1),
                  "bps_yoy": round((ebitda[-1] / rev[-1] - ebitda[-5] / rev[-5]) * 10000)}
    # surprise: this quarter's PAT YoY vs the average of the previous 4 quarters' YoY
    trend = None
    if pat and len(pat) >= 9:
        yoys = [pct(pat[i], pat[i - 4]) for i in range(len(pat) - 5, len(pat) - 1)]
        yoys = [y for y in yoys if y is not None]
        if yoys:
            trend = round(sum(yoys) / len(yoys), 1)
    return {"quarter": fy_quarter(cols[-1]), "prev": cols[-2], "yago": cols[-5], "rows": rows,
            "margin": margin, "pat_trend_yoy": trend, "unit": "₹ cr",
            "source": scr["url"] + "#quarters"}


BOILER = re.compile(r"kindly take|thanking you|yours (faithfully|sincerely|truly)|request you to|take (the same|this) on record|"
                     r"enclosed (herewith|as)|please find|for your (information|records)|debarred|\bDIN\b|inter-se|"
                     r"true, correct and complete|is also being uploaded|in the manner as prescribed", re.I)
LEAD = re.compile(r"^(in (furtherance|continuation|terms) of[^,]*,\s*|pursuant to[^,]*,\s*|further to[^,]*,\s*)?"
                  r"(we (wish|would like|hereby|are pleased|write) to (inform|submit|intimate)( you)?( that)?|"
                  r"this is to inform( you)? that|you are kindly informed that|we hereby inform( you)? that)\s*", re.I)


def gist(f, n=2):
    """The first informative sentences of the filing letter, letterhead and legalese removed."""
    if "_gist" in f:
        return f["_gist"]
    t = " ".join(pdf_text(f.get("pdf"), max_pages=3).split())
    m = re.search(r"dear\s+sir[^,]{0,20},?", t, re.I)
    body = t[m.end():] if m else t
    head = body[:450]
    cut = list(re.finditer(r"regulations?,?\s*\d{4}\s*[\"”’)]*\)?", head, re.I))
    if cut and re.match(r"\s*sub", body, re.I):
        body = body[cut[-1].end():]
        body = re.sub(r"^\s*[-–:]\s*[^.]{0,120}?(?=\s(?:Pursuant|In terms|We |This |Further|In furtherance|In continuation|The Board|Dear))", "", body)
    sents = re.split(r"(?<=[a-z0-9)%])\.\s+(?=[A-Z(])", body)
    keep = []
    for x in sents:
        x = x.strip()
        if len(x) < 30 or BOILER.search(x):
            continue
        if re.fullmatch(r"[^.]*regulation 30[^.]*", x, re.I) and len(x) < 160:
            continue
        x = LEAD.sub("", x)
        x = x[:1].upper() + x[1:]
        keep.append(x if len(x) < 420 else x[:400].rsplit(" ", 1)[0] + "…")
        if len(keep) >= n:
            break
    f["_gist"] = ". ".join(k.rstrip(".") for k in keep) + ("." if keep else "")
    return f["_gist"]


def annexure_fields(f):
    """SEBI's Schedule III annexures are label/value tables. Pull the ones an investor reads."""
    t = " ".join(pdf_text(f.get("pdf"), max_pages=4).split())
    out = {"_text": t}
    m = re.search(r"name of the authority\s*:?\s*(.{3,90}?)\s*(?:\d\s*)?(?:nature and details|date of receipt)", t, re.I)
    if m:
        out["authority"] = re.sub(r"\s+\d$", "", m.group(1)).strip(" .:-")
    m = re.search(r"nature and details of the action\(s\) taken or order\(s\) passed\s*:?\s*(.{5,220}?)\s*(?:\d\s*)?date of receipt", t, re.I)
    if m:
        out["action"] = m.group(1).strip(" .:-")
    m = re.search(r"to the extent possible\s*:?\s*(.{10,260}?)(?:\.|$)", t, re.I)
    if m:
        out["impact"] = m.group(1).strip()
    if re.search(r"in favou?r of the company|positive (tax )?impact|set(ting)? aside", t, re.I):
        out["direction"] = "favourable"
    elif re.search(r"penalt|disincentive|fine|demand", t, re.I):
        out["direction"] = "adverse"
    return out


def mgmt_block(f):
    """Who, role, in/out, and a short background from the filing's own profile."""
    g = gist(f)
    blob = " ".join([f["summary"], f["text"], g])
    names = re.findall(r"\b(?:Mr|Ms|Mrs|Dr|Prof|Shri|Smt)\.?\s+((?:[A-Z][a-z]*\.?\s+){0,3}[A-Z][a-z]+)", blob)
    names = [n.strip() for n in names if n.strip().lower() not in ("sir", "madam")]
    role, act = (f.get("subtype") or "Senior management|changes").split("|")
    # re-derive role/action from the letter itself when the exchange subject was generic
    low = re.split(r"(?<=\.)\s", g, maxsplit=1)[0].lower() if g else ""
    low = re.sub(r"board of directors|directors of the (company|bank)|nomination and remuneration", "", low)
    for r, rx in ROLE_RX:
        if re.search(rx, low):
            role = r
            break
    m = re.search(r"chief [a-z ]{3,30}officer|director \([a-z ]+\)|company secretary", g.lower() if g else "")
    title = m.group(0).title() if m else None
    if re.search(r"resign", low):
        act = "resigns"
    elif re.search(r"appoint|assumed office|joins|joined", low):
        act = "appointed"
    elif re.search(r"retire|cease|completed .{0,20}term|superannuat", low):
        act = "steps down"
    f["subtype"] = f"{role}|{act}"
    subsidiary = bool(re.search(r"subsidiary", blob, re.I)) and not re.search(r"of the (company|bank)", low)
    bio = None
    if act == "appointed":
        bio = profile_from_pdf(f["pdf"], names[0] if names else None)
    tenure = None
    m = re.search(r"(\d+(?:\.\d+)?)\s+years", blob)
    if m:
        tenure = m.group(0)
    abrupt = act == "resigns" and bool(re.search(r"personal reasons|with immediate effect|pursue other|before the end of", blob, re.I))
    return {"person": names[0] if names else None, "role": role, "title": title, "action": act,
            "bio": bio, "experience": tenure, "abrupt": abrupt, "subsidiary": subsidiary}


BIO_RX = re.compile(r"experience|graduat|degree|engineer|chartered|joined|career|served|worked|alumn|mba|b\.?\s?tech|"
                    r"post.?graduate|qualified|prior to|previously|has been (with|associated)|over \d+ years|decades", re.I)


def profile_from_pdf(url, name=None):
    """SEBI LODR requires a 'brief profile' in appointment filings. Pull it out."""
    pages = " ".join(" ".join(pdf_text(url, max_pages=6).split()).split())
    if not pages:
        return None
    start = 0
    for m in re.finditer(r"brief profile|profile of", pages, re.I):
        start = m.end()  # the last mention is usually the annexure heading
    text = pages[start:] if start else pages
    sents = re.split(r"(?<=[a-z0-9)])\.\s+(?=[A-Z])", text)
    picked = [x.strip() for x in sents if BIO_RX.search(x) and not BOILER.search(x) and 40 < len(x) < 500]
    if not picked:
        return None
    short = ". ".join(picked[:2])
    short = llm.maybe_compress(short, "Rewrite as a 2-line professional background: past firms, roles, years.") or short
    return short[:420].rsplit(" ", 1)[0].rstrip(" ,;") + "."


def magnitude_block(f, scr):
    """Express size relative to the company: the trick that makes a rupee figure mean something."""
    blob = " ".join([f["subject"], f["text"], f["summary"]])
    amt = amount_cr(blob)
    band = None
    if amt is None and f["symbol"] == "LT":
        m = re.search(r"\b(ultra-mega|mega|major|large|significant)\b", blob, re.I)
        if m:
            band = m.group(1).lower()
            lo, hi = LT_ORDER_BANDS[band]
            amt = lo if hi is None else (lo + hi) / 2
    if amt is None or not scr:
        return None
    rev_ttm = (series(scr.get("pl"), "Sales", "Revenue") or [None])[-1]
    mcap = None
    m = re.search(r"([\d,]+)", scr.get("top", {}).get("Market Cap", ""))
    if m:
        mcap = float(m.group(1).replace(",", ""))
    out = {"amount_cr": round(amt), "band": band}
    if rev_ttm:
        out["pct_revenue"] = round(amt / rev_ttm * 100, 1)
    if mcap:
        out["pct_mcap"] = round(amt / mcap * 100, 2)
    return out


# ------------------------------------------------------------------ prices

def price_features(rows, index_rows):
    """Per-date return, volume multiple vs prior 20-day average, move relative to Nifty."""
    idx = {r["date"]: r["close"] for r in (index_rows or [])}
    feats = {}
    for i in range(21, len(rows)):
        r, p = rows[i], rows[i - 1]
        avg20 = sum(x["volume"] for x in rows[i - 20:i]) / 20 or 1
        ret = (r["close"] / p["close"] - 1) * 100
        iret = None
        if r["date"] in idx and p["date"] in idx:
            iret = (idx[r["date"]] / idx[p["date"]] - 1) * 100
        feats[r["date"]] = {"ret": round(ret, 2), "vol_x": round(r["volume"] / avg20, 2),
                            "rel": round(ret - iret, 2) if iret is not None else None}
    return feats


def reaction(feats, when):
    """Market reaction on the filing day or the next trading day."""
    # a filing after the 15:30 close can only move the next session
    d0 = when.date() + timedelta(days=1) if (when.hour, when.minute) >= (15, 30) else when.date()
    best = None
    for k in sorted(feats):
        d = datetime.strptime(k, "%Y-%m-%d").date()
        if d0 <= d <= d0 + timedelta(days=4):
            best = (k, feats[k])
            break
    return best


# ------------------------------------------------------------------ score

BASE = {
    "results": 45, "M&A": 35, "fund_raise": 35, "litigation": 30, "insider_trading": 25,
    "earnings_call": 12, "investor_meetings": 4, "credit_rating": 25, "dividend": 12, "bonus_split": 18,
    "capex": 30, "press_release": 14, "postal_ballot": 4, "agm_egm": 4, "pledging": 40, "unclassified": 0,
}
ROLE_BASE = {"CEO": 55, "MD": 55, "CFO": 50, "Chairman": 45, "Vice Chairman": 32, "Director": 22,
             "Independent Director": 14, "Company Secretary": 10, "Senior management": 22}
LABEL = {
    "results": "Results", "M&A": "M&A", "fund_raise": "Fund raise", "litigation": "Legal / regulatory",
    "insider_trading": "Insider trade", "earnings_call": "Earnings call", "investor_meetings": "Investor meet",
    "credit_rating": "Credit rating", "dividend": "Dividend", "bonus_split": "Bonus / split", "capex": "Capex",
    "press_release": "Update", "postal_ballot": "Postal ballot", "agm_egm": "AGM / EGM", "pledging": "Pledge",
    "unclassified": "Routine", "management_change": "Management", "price": "Price move",
}


def score(ev, weight):
    cat, sub = ev["category"], ev.get("subtype")
    reasons = []
    if cat == "management_change":
        role, act = sub.split("|")
        base = ROLE_BASE.get(role, 20)
        reasons.append(f"{role} {act}")
    else:
        base = BASE.get(cat, 10)
        if cat == "credit_rating":
            base = {"downgrade": 55, "upgrade": 30, "reaffirm": 6}.get(sub, 20)
            reasons.append(f"Rating {sub}")
        if cat == "M&A" and sub in ("new_subsidiary", "strike_off"):
            base = 8
        if cat == "fund_raise" and sub == "buyback":
            base = 45
        if sub == "order_win":
            base = 22
        if cat == "litigation":
            L = ev.get("legal") or {}
            amt = (ev.get("magnitude") or {}).get("pct_revenue")
            if amt is None or amt < 0.5:
                base = 14
                reasons.append("No material amount")
            elif L.get("direction") == "favourable":
                base = 30
                reasons.append("Ruling in company's favour")
        if sub == "clarification":
            base = 28
            reasons.append("Exchange sought clarification")
        if sub == "deck":
            base = 8

    mag = 1.0
    m = ev.get("magnitude")
    if m:
        r = (m.get("pct_revenue") or 0) / 100
        mag = max(0.7, min(2.0, 0.7 + 5 * r))
        if m.get("pct_revenue") is not None:
            reasons.append(f"{m['pct_revenue']:g}% of revenue")

    sur = 1.0
    res = ev.get("results")
    if res:
        pat = next((r for r in res["rows"] if r["label"] == "PAT"), None)
        if pat and pat["yoy"] is not None:
            trend = res.get("pat_trend_yoy") or 0
            dev = pat["yoy"] - trend
            sur = 0.8 + min(0.7, abs(dev) / 40)  # in-line results matter less than surprises
            reasons.append(f"PAT {pat['yoy']:+.0f}% YoY")
            if abs(dev) >= 10:
                reasons.append(("Above" if dev > 0 else "Below") + f" trend ({trend:+.0f}%)")
    mg = ev.get("management")
    if mg and mg.get("abrupt"):
        sur = 1.3
        reasons.append("Abrupt exit")

    pos = 0.75 + 0.04 * weight
    react = 0
    rx = ev.get("reaction")
    if rx and base >= 20:  # a price move only lifts events that could plausibly cause it
        if rx["vol_x"] >= 2:
            react += 12
            reasons.append(f"Volume {rx['vol_x']:.1f}×")
        if rx.get("rel") is not None and abs(rx["rel"]) >= 3:
            react += 10
            reasons.append(f"{rx['ret']:+.1f}% on the day")
    if mg and mg.get("subsidiary"):
        base = base * 0.5
    clus = ev.get("cluster_bonus", 0) if base >= 20 else 0
    s = base * mag * sur * pos + react + clus
    if clus:
        reasons.append("Several events this week")
    ev["score"] = int(max(0, min(100, round(s))))
    ev["reasons"] = reasons[:3]
    ev["factors"] = {"base": base, "magnitude": round(mag, 2), "surprise": round(sur, 2),
                     "position": round(pos, 2), "reaction": react, "cluster": ev.get("cluster_bonus", 0)}
    return ev


# ------------------------------------------------------------------ so-what line

def _trim(t, n):
    w = re.sub(r"\s+", " ", t).strip(" .").split()
    return " ".join(w[:n]) + ("…" if len(w) > n else "")


def so_what(ev):
    cat = ev["category"]
    res, mg, m = ev.get("results"), ev.get("management"), ev.get("magnitude")
    if res:
        rows = {r["label"]: r for r in res["rows"]}
        rev, pat = rows.get("Revenue"), rows.get("PAT")
        bits = []
        if rev and rev["yoy"] is not None:
            bits.append(f"sales {'up' if rev['yoy'] >= 0 else 'down'} {abs(rev['yoy']):.0f}%")
        if pat and pat["yoy"] is not None:
            bits.append(f"profit {'up' if pat['yoy'] >= 0 else 'down'} {abs(pat['yoy']):.0f}%")
        return f"{res['quarter']} results: " + " and ".join(bits) + " on a year ago"
    if mg:
        who = mg["person"] or "A senior executive"
        role = mg.get("title") or mg["role"]
        where = " at a subsidiary" if mg.get("subsidiary") else ""
        return f"{who}, {role}{where}, {mg['action']}"
    if m and m.get("pct_revenue") is not None:
        lead = "Order" if ev.get("subtype") == "order_win" else "Deal"
        amt = f"₹{m['amount_cr']:,} cr" + (f" ({m['band']} band)" if m.get("band") else "")
        return f"{lead} worth {amt}, {m['pct_revenue']:g}% of annual revenue"
    if ev.get("legal") and m and m.get("amount_cr"):
        L = ev["legal"]
        who = L.get("authority") or "Legal order"
        fav = " in the company's favour" if L.get("direction") == "favourable" else ""
        return _trim(f"{who}: ₹{m['amount_cr']:,} cr at stake{fav}, {m['pct_revenue']:g}% of annual revenue", 24)
    if ev.get("legal") and (ev["legal"].get("authority") or ev["legal"].get("action")):
        L = ev["legal"]
        bits = [L.get("authority"), L.get("action")]
        line = " · ".join(b for b in bits if b)
        if m and m.get("amount_cr"):
            line += f" · ₹{m['amount_cr']:,} cr" + (" in the company's favour" if L.get("direction") == "favourable" else "")
        return _trim(line, 24)
    # Exchange one-liner when it says something; otherwise the first line of the letter itself.
    t = ev["text"] or ev["subject"]
    if (len(t) < 45 or t.lower().strip(" .") == ev["subject"].lower().strip(" .")) and ev.get("gist"):
        t = ev["gist"]
    t = re.sub(r"\s+", " ", t).strip(" .")
    words = t.split()
    return " ".join(words[:18]) + ("…" if len(words) > 18 else "")


# ------------------------------------------------------------------ build events

def build_events(filings, screeners, prices, index_rows, weights):
    feats = {s: price_features(rows, index_rows) for s, rows in prices.items() if rows}
    evs = []
    for f in filings:
        cat, sub = classify(f)
        ev = dict(f, category=cat, subtype=sub, label=LABEL.get(cat, cat))
        scr = screeners.get(f["symbol"])
        if cat == "results" and sub == "results":
            ev["results"] = results_block(scr)
        if cat == "management_change":
            ev["management"] = mgmt_block(ev)
        if cat in ("M&A", "press_release", "litigation", "fund_raise", "capex", "credit_rating", "management_change", "pledging", "insider_trading") \
                and sub not in ("deck",):
            ev["gist"] = gist(ev)
        extra = ""
        if cat == "litigation":
            ev["legal"] = annexure_fields(ev)
            extra = ev["legal"].pop("_text", "")
        if cat == "credit_rating" and ev.get("gist"):
            ev["subtype"] = _subtype(cat, None, dict(f, summary=ev["gist"]))[1]
        if cat in ("M&A", "press_release", "litigation", "fund_raise", "capex"):
            ev["magnitude"] = magnitude_block(dict(f, summary=f["summary"] + " " + ev.get("gist", "") + " " + extra), scr)
        rx = reaction(feats.get(f["symbol"], {}), datetime.fromisoformat(f["time"]))
        if rx:
            ev["reaction"] = dict(rx[1], date=rx[0])
        evs.append(ev)

    evs = merge_results_days(evs)

    # cluster bonus: 2+ non-routine events on one stock within 7 days
    by_sym = defaultdict(list)
    for e in evs:
        if BASE.get(e["category"], 20) >= 12 or e["category"] == "management_change":
            by_sym[e["symbol"]].append(e)
    for sym, lst in by_sym.items():
        for e in lst:
            t = datetime.fromisoformat(e["time"])
            near = [o for o in lst if o is not e and abs((datetime.fromisoformat(o["time"]) - t).days) <= 7]
            e["cluster_bonus"] = 8 if near else 0

    for e in evs:
        score(e, weights.get(e["symbol"], 3))
        e["so_what"] = so_what(e)
    return sorted(evs, key=lambda e: (-e["score"], e["time"]))


def merge_results_days(evs):
    """A results day produces 4 to 6 filings (outcome, press release, deck, newspaper ad, call
    recording). Fold everything within 3 days of the results filing into one card."""
    res_rx = re.compile(r"financial results|unaudited|audited|results for the (quarter|half)", re.I)
    anchors = []
    for e in sorted((e for e in evs if e["category"] == "results" and e.get("results")), key=lambda e: e["time"]):
        if not any(a["symbol"] == e["symbol"] and abs((datetime.fromisoformat(e["time"]) - datetime.fromisoformat(a["time"])).days) <= 3 for a in anchors):
            anchors.append(e)
    dupes = [e for e in evs if e["category"] == "results" and e.get("results") and e not in anchors]
    for d in dupes:
        d.pop("results", None)
    out = []
    for e in evs:
        if e in anchors:
            continue
        is_res_like = e["category"] in ("results", "earnings_call") or (
            e["category"] in ("press_release", "unclassified") and res_rx.search(e["subject"] + " " + e["text"]))
        home = None
        if is_res_like:
            t = datetime.fromisoformat(e["time"])
            home = next((a for a in anchors if a["symbol"] == e["symbol"] and
                         -1 <= (t - datetime.fromisoformat(a["time"])).days <= 3), None)
        if home:
            home.setdefault("related", []).append({"subject": e["subject"], "text": e["text"][:140], "pdf": e["pdf"], "time": e["time"]})
        else:
            if e["category"] == "results" and not e.get("results"):
                e["category"], e["subtype"], e["label"] = "unclassified", "results_paperwork", "Routine"
            out.append(e)
    return out + anchors


def price_events(prices, index_rows, weights, names, since):
    """EOD threshold candidates. The app re-applies the investor's own thresholds."""
    out = []
    for s, rows in prices.items():
        if not rows:
            continue
        for d, f in price_features(rows, index_rows).items():
            if d < since:
                continue
            if abs(f["ret"]) >= 3 or f["vol_x"] >= 1.8:
                out.append({"id": f"px-{s}-{d}", "symbol": s, "company": names.get(s, s), "date": d,
                            "ret": f["ret"], "vol_x": f["vol_x"], "rel": f["rel"]})
    return out
