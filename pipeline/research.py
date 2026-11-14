"""Pull side: a research view for any NSE ticker, grounded in primary sources only.

Numbers: computed in code from screener.in's standardised tables, which are
built from the company's own annual reports and quarterly filings.
Words: guidance is read from the company's own concall transcripts. Every row
carries a link to the document it came from. Nothing comes from the model's
own knowledge, news or broker notes.
"""
import json
import os
import re

from bs4 import BeautifulSoup

from . import llm, news, reality
from .sources import ROOT, ist_now, live_quote, pdf_pages, screener, series

GUIDANCE_DIR = os.path.join(ROOT, "data", "guidance")


def cagr(a, b, years):
    if not a or not b or a <= 0 or b <= 0 or years <= 0:
        return None
    return round(((b / a) ** (1 / years) - 1) * 100, 1)


def period_label(c):
    m = re.match(r"(\w{3}) (\d{4})", c)
    if not m:
        return c
    mon, yr = m.groups()
    return f"FY{yr[2:]}" if mon == "Mar" else f"{mon} '{yr[2:]}"


def last(v, n=1):
    v = [x for x in (v or []) if x is not None]
    return v[-n] if len(v) >= n else None


def segments(scr):
    """Clean screener's commentary block (sourced from company fact sheets)."""
    note = scr.get("segments_note") or ""
    note = re.sub(r"\[\d+\]", "", note)
    lines, buf = [], ""
    for raw in note.split("\n"):
        s = raw.strip()
        if not s:
            continue
        if s.startswith("-") or s.endswith(":"):
            if buf:
                lines.append(buf.strip())
            buf = s
        else:
            buf += " " + s
    if buf:
        lines.append(buf.strip())
    out = []
    for ln in lines[:12]:
        m = re.match(r"-\s*(.+?):\s*([\d.]+)%", ln)
        if m:
            out.append({"name": m.group(1).strip(), "pct": float(m.group(2))})
    heading = next((l.rstrip(":") for l in lines if l.endswith(":")), None)
    return {"title": heading, "items": out, "source": scr.get("segments_src")}


def build(symbol):
    scr = screener(symbol)
    if not scr or not scr.get("pl"):
        return {"symbol": symbol, "error": f"No company found for {symbol}. Check the NSE ticker, for example TRENT or NESTLEIND."}

    pl, bs, cf, rt, q = scr["pl"], scr["bs"], scr["cf"], scr["ratios"], scr["quarters"]
    is_fin = series(pl, "Financing Profit") is not None
    years = [c for c in pl["cols"] if c != "TTM"]
    n_y = len(years)
    rev = series(pl, "Sales", "Revenue")[:n_y]
    op = (series(pl, "Operating Profit", "Financing Profit") or [])[:n_y]
    pat = series(pl, "Net Profit")[:n_y]
    cfo = series(cf, "Cash from Operating")
    fcf = series(cf, "Free Cash Flow")
    cf_years = cf["cols"] if cf else []
    eq = [a + b if a is not None and b is not None else None
          for a, b in zip(series(bs, "Equity Capital") or [], series(bs, "Reserves") or [])]
    debt = series(bs, "Borrowings", "Borrowing")
    bs_cols = bs["cols"] if bs else []
    wc_days = series(rt, "Working Capital Days")
    debtor_days = series(rt, "Debtor Days")
    roce = series(rt, "ROCE %")
    roe = series(rt, "ROE %")
    promoter = series(scr.get("holding"), "Promoters")
    holding_cols = scr["holding"]["cols"] if scr.get("holding") else []

    src_pl = scr["url"] + "#profit-loss"
    src_bs = scr["url"] + "#balance-sheet"
    src_cf = scr["url"] + "#cash-flow"
    ar = scr["docs"]["annual_reports"][:1]
    ar_src = ar[0]["url"] if ar else src_pl

    # -------- trajectory (last 6 fiscal years)
    k = min(6, n_y)
    traj = {
        "years": [period_label(y) for y in years[-k:]],
        "revenue": rev[-k:], "op_margin": [round(o / r * 100, 1) if o and r else None for o, r in zip(op[-k:], rev[-k:])],
        "pat": pat[-k:], "op_label": "Financing margin" if is_fin else "EBITDA margin",
        "rev_cagr_5y": cagr(rev[-6], rev[-1], 5) if n_y >= 6 else None,
        "rev_cagr_3y": cagr(rev[-4], rev[-1], 3) if n_y >= 4 else None,
        "pat_cagr_5y": cagr(pat[-6], pat[-1], 5) if n_y >= 6 else None,
        "pat_cagr_3y": cagr(pat[-4], pat[-1], 3) if n_y >= 4 else None,
        "source": src_pl,
    }

    # -------- cash quality: does profit turn into cash?
    conv = []
    for i, y in enumerate(cf_years[-k:]):
        j = years.index(y) if y in years else None
        c = cfo[-k:][i] if cfo else None
        p = pat[j] if j is not None else None
        conv.append(round(c / p * 100) if c is not None and p else None)
    conv3 = [x for x in conv[-3:] if x is not None]
    cash = {
        "years": [period_label(y) for y in cf_years[-k:]],
        "cfo": (cfo or [])[-k:], "fcf": (fcf or [])[-k:], "cfo_to_pat": conv,
        "avg3": round(sum(conv3) / len(conv3)) if conv3 else None,
        "fcf_sum_3y": round(sum(x for x in (fcf or [])[-3:] if x is not None)) if fcf else None,
        "applicable": not is_fin, "source": src_cf,
    }

    # -------- balance sheet
    de = [round(d / e, 2) if d is not None and e else None for d, e in zip(debt or [], eq)]
    balance = {
        "cols": [period_label(c) for c in bs_cols[-5:]],
        "debt": (debt or [])[-5:], "equity": eq[-5:], "de": de[-5:],
        "wc_days": (wc_days or [])[-5:], "debtor_days": (debtor_days or [])[-5:],
        "promoter": {"cols": holding_cols[-5:], "pct": (promoter or [])[-5:]},
        "is_financial": is_fin, "source": src_bs,
    }

    # -------- headline snapshot
    top = scr.get("top", {})
    ttm = pl["cols"][-1] == "TTM"
    snap = {
        "price": top.get("Current Price"), "mcap": top.get("Market Cap"), "pe": top.get("Stock P/E"),
        "roce": top.get("ROCE"), "roe": top.get("ROE"),
        "revenue_ttm": series(pl, "Sales", "Revenue")[-1] if ttm else rev[-1],
        "pat_ttm": series(pl, "Net Profit")[-1] if ttm else pat[-1],
        "op_margin_ttm": None,
    }
    o_ttm = (series(pl, "Operating Profit", "Financing Profit") or [None])[-1]
    if o_ttm and snap["revenue_ttm"]:
        snap["op_margin_ttm"] = round(o_ttm / snap["revenue_ttm"] * 100, 1)

    # -------- quarterly (for promised vs delivered)
    qrev = series(q, "Sales", "Revenue")
    qop = series(q, "Operating Profit", "Financing Profit")
    qpat = series(q, "Net Profit")
    quarters = {"cols": q["cols"][-8:], "revenue": qrev[-8:], "op": (qop or [])[-8:], "pat": qpat[-8:],
                "source": scr["url"] + "#quarters"}

    guidance = load_guidance(symbol, scr, quarters)

    research = {
        "symbol": symbol, "name": scr["name"], "about": first_sentences(scr.get("about", ""), 2),
        "segments": segments(scr), "snapshot": snap, "trajectory": traj, "cash": cash,
        "balance": balance, "quarters": quarters, "guidance": guidance,
        "sources": {
            "screener": scr["url"], "annual_report": ar[0] if ar else None,
            "transcripts": [c for c in scr["docs"]["concalls"] if c.get("transcript")][:4],
        },
        "is_financial": is_fin,
    }
    research["bull"], research["bear"] = bull_bear(research)
    research["verdict"] = verdict_line(research)

    # what they say vs what the numbers show, and whether the profit is real
    research["claims"] = _safe(lambda: reality.claims_check(scr, is_fin), {"items": [], "checked": 0})
    research["quality"] = _safe(lambda: reality.profit_quality(scr, is_fin), {"checks": []})
    # live price and headlines: context only, never used in the analysis above
    research["quote"] = _safe(lambda: live_quote(symbol), None)
    research["news"] = _safe(lambda: news.headlines(symbol, scr["name"], days=7, limit=5), [])
    research["generated"] = ist_now().isoformat(timespec="minutes")
    return research


def _safe(fn, default):
    try:
        return fn()
    except Exception as ex:  # one weak source must not sink the page
        print("research step failed:", ex)
        return default


def first_sentences(t, n):
    s = re.split(r"(?<=[.!?])\s+", t.strip())
    return " ".join(s[:n])


# ------------------------------------------------------------------ guidance

GUIDE_RX = re.compile(
    r"([^.?!]{0,220}\b(guid(?:e|ance|ing)|expect|target|aspir|we will|we should|going forward|next year|"
    r"for the year|full[- ]year|fy ?2\d|by fy|band|range of|in the range)\b[^.?!]{0,220}"
    r"\d[\d,.]*\s*(?:%|percent|per cent|crore|cr\b|bps|basis points|stores|x\b)[^.?!]{0,160}[.?!])", re.I)


def transcript_snippets(url, limit=14):
    """Cheap pre-filter: only sentences that look like numeric forward guidance go to a model."""
    pages = pdf_pages(url, max_pages=40)
    out = []
    for pi, page in enumerate(pages, 1):
        for m in GUIDE_RX.finditer(" ".join(page.split())):
            s = m.group(1).strip()
            if len(s) > 40:
                out.append((pi, s))
    seen, uniq = set(), []
    for p, s in out:
        key = s[:60]
        if key not in seen:
            seen.add(key)
            uniq.append((p, s))
    return uniq[:limit]


def load_guidance(symbol, scr, quarters):
    """1) cached extraction for this ticker  2) model extraction if a key is set
    3) otherwise raw numeric quotes from the transcripts, clearly marked unverified."""
    path = os.path.join(GUIDANCE_DIR, f"{symbol}.json")
    if os.path.exists(path):
        g = json.load(open(path))
        g["mode"] = "extracted"
        return g
    calls = [c for c in scr["docs"]["concalls"] if c.get("transcript")][:4]
    items = []
    for c in (calls if llm.KEY else []):  # without a model key, skip straight to the quotes below
        snips = transcript_snippets(c["transcript"])
        rows = llm.extract_guidance(c["period"], snips)
        for r in rows:
            r["source"] = c["transcript"]
        items.extend(rows)
    if items:
        return {"mode": "extracted", "items": items}
    # no model available: show the candidate sentences themselves, linked, no verdicts
    quotes = []
    for c in calls[:2]:
        for p, s in transcript_snippets(c["transcript"], limit=4):
            quotes.append({"said_in": c["period"], "quote": s, "page": p, "source": c["transcript"]})
    return {"mode": "quotes", "items": quotes, "note": "Numeric forward-looking lines found in the latest transcripts. "
            "Verdicts need the extraction step (set a model key) or a cached extraction."}


# ------------------------------------------------------------------ bull / bear (rules over computed numbers)

def bull_bear(r):
    t, c, b = r["trajectory"], r["cash"], r["balance"]
    bull, bear = [], []
    src_pl, src_cf, src_bs = t["source"], c["source"], b["source"]

    rc = t.get("rev_cagr_5y") or t.get("rev_cagr_3y")
    span = "5" if t.get("rev_cagr_5y") is not None else "3"
    if rc is not None and rc >= 12:
        bull.append({"text": f"Revenue has compounded {rc:g}% a year over {span} years", "src": src_pl})
    elif rc is not None and rc < 7:
        bear.append({"text": f"Revenue grew only {rc:g}% a year over {span} years", "src": src_pl})
    r5, r3 = t.get("rev_cagr_5y"), t.get("rev_cagr_3y")
    if r5 is not None and r3 is not None and r5 - r3 >= 3:
        bear.append({"text": f"Growth is slowing: revenue {r3:g}% a year over 3 years vs {r5:g}% over 5", "src": src_pl})
    elif r5 is not None and r3 is not None and r3 - r5 >= 3:
        bull.append({"text": f"Growth is speeding up: revenue {r3:g}% a year over 3 years vs {r5:g}% over 5", "src": src_pl})
    pc = t.get("pat_cagr_5y") or t.get("pat_cagr_3y")
    if pc is not None and rc is not None and pc - rc >= 4:
        bull.append({"text": f"Profit grew faster than sales ({pc:g}% vs {rc:g}% a year), so margins widened", "src": src_pl})
    elif pc is not None and rc is not None and rc - pc >= 4:
        bear.append({"text": f"Profit lagged sales ({pc:g}% vs {rc:g}% a year), so margins compressed", "src": src_pl})

    m = [x for x in t["op_margin"] if x is not None]
    if len(m) >= 3:
        d = m[-1] - m[-3]
        if d >= 1.5:
            bull.append({"text": f"{t['op_label']} up {d:.1f} pts in two years to {m[-1]:g}%", "src": src_pl})
        elif d <= -1.5:
            bear.append({"text": f"{t['op_label']} down {abs(d):.1f} pts in two years to {m[-1]:g}%", "src": src_pl})

    if c["applicable"] and c.get("avg3") is not None:
        if c["avg3"] >= 90:
            bull.append({"text": f"Profits turn into cash: operating cash flow was {c['avg3']}% of PAT over 3 years", "src": src_cf})
        elif c["avg3"] < 70:
            bear.append({"text": f"Weak cash conversion: operating cash flow only {c['avg3']}% of PAT over 3 years", "src": src_cf})
    if c["applicable"] and c.get("fcf_sum_3y") is not None and c["fcf_sum_3y"] < 0:
        bear.append({"text": f"Free cash flow was negative over 3 years (₹{c['fcf_sum_3y']:,} cr)", "src": src_cf})

    de = [x for x in b["de"] if x is not None]
    if de and not b["is_financial"]:
        if de[-1] <= 0.1:
            bull.append({"text": f"Almost no debt: debt-to-equity {de[-1]:g}", "src": src_bs})
        elif de[-1] >= 1:
            bear.append({"text": f"Leveraged: debt-to-equity {de[-1]:g}", "src": src_bs})
        elif len(de) >= 3 and de[-1] - de[-3] >= 0.25:
            bear.append({"text": f"Debt rising: debt-to-equity went from {de[-3]:g} to {de[-1]:g}", "src": src_bs})
    wc = [x for x in b["wc_days"] if x is not None]
    if len(wc) >= 3 and wc[-1] - wc[-3] >= 15:
        bear.append({"text": f"Working capital days stretched from {wc[-3]:g} to {wc[-1]:g}", "src": src_bs})
    elif len(wc) >= 3 and wc[-3] - wc[-1] >= 15:
        bull.append({"text": f"Working capital days fell from {wc[-3]:g} to {wc[-1]:g}", "src": src_bs})
    dd = [x for x in b["debtor_days"] if x is not None]
    if len(dd) >= 3 and dd[-1] - dd[-3] >= 10:
        bear.append({"text": f"Customers are paying slower: debtor days rose from {dd[-3]:g} to {dd[-1]:g}", "src": src_bs.replace("#balance-sheet", "#ratios")})
    pr = [x for x in b["promoter"]["pct"] if x is not None]
    if len(pr) >= 2 and pr[-1] - pr[0] <= -2:
        bear.append({"text": f"Promoter holding fell from {pr[0]:g}% to {pr[-1]:g}%", "src": src_bs.replace("#balance-sheet", "#shareholding")})

    g = r["guidance"]
    if g.get("mode") == "extracted" and g.get("items"):
        good = [x for x in g["items"] if x.get("verdict") in ("kept", "on track")]
        bad = [x for x in g["items"] if x.get("verdict") in ("missed", "moved")]
        if good and len(good) > len(bad):
            bull.insert(0, {"text": f"Management is delivering: {len(good)} of {len(good) + len(bad) + sum(1 for x in g['items'] if x.get('verdict') == 'partly')} checkable promises kept or on track", "src": good[0].get("actual_source") or good[0].get("source")})
        for x in bad[:2]:
            bear.insert(0, {"text": f"{x['metric']}: promised {x['promise']}, " + (f"got {x['actual']}" if x.get("actual") else "not delivered"), "src": x.get("actual_source") or x.get("source")})
    return bull[:3], bear[:3]


def verdict_line(r):
    g = r["guidance"]
    pts = {"kept": 1, "on track": 1, "partly": 0.5, "missed": 0, "moved": 0}
    graded = [x for x in g.get("items", []) if x.get("verdict") in pts]
    if not graded:
        return {"credibility": None}
    score = sum(pts[x["verdict"]] for x in graded)
    return {"credibility": {"score": score, "of": len(graded),
                            "counts": {k: sum(1 for x in graded if x["verdict"] == k) for k in pts}}}
