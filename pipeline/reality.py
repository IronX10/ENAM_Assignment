"""What the company says vs what its numbers show.

Two checks, both computed in code with no model:

1. Claims check. Management opens every earnings call by reading out the
   quarter's numbers ("revenue grew 11.2% year on year", "EBITDA margin was
   14.4%", "net profit of Rs 3,224 crore"). FINN pulls those sentences from the
   company's own transcript and recomputes each figure from the reported
   quarterly results. Every row carries the quote, the page and both sources.

2. Profit quality. Seven rules over the cash flow, balance sheet and results
   tables that look for the usual gaps between reported profit and the
   business underneath it: profit that does not turn into cash, customers
   paying slower, profit propped up by other income or a low tax rate, debt
   climbing, promoters selling.
"""
import re

from .sources import pdf_pages, screener, series

MONTHS = {m: i for i, m in enumerate("Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(), 1)}

# ------------------------------------------------------------------ claims

METRIC_RX = [
    ("pat", re.compile(r"\b(profit after tax|PAT|net profit|bottom[- ]line|net income)\b", re.I)),
    ("pbt", re.compile(r"\b(profit before tax|PBT)\b", re.I)),
    ("ebitda", re.compile(r"\b(EBITDA|operating profit)\b", re.I)),
    ("revenue", re.compile(r"\b(revenues?|sales|top[- ]line|income from operations|total income)\b", re.I)),
]
LABEL = {"revenue": "Revenue", "ebitda": "EBITDA", "pbt": "Profit before tax", "pat": "Net profit", "margin": "EBITDA margin"}

# The figure has to be for the whole company and for the quarter, or it cannot be checked against the quarter.
# The words just before the metric may only be these; anything else ("India formulations", "Home appliances",
# "manufacturing", "Avenue E-Commerce") means the figure is for a part of the company.
SUBJECT_OK = set("""consolidated total overall our the company's company group quarterly net reported and also this
quarter's quarter q1 q2 q3 q4 at in on of terms level basis we have has achieved delivered recorded posted saw had
generated its registered a with both for operational so then further i am happy to report that is was
key highlights as below highest ever record strong healthy robust""".split())
PART_RX = re.compile(
    r"\b(segments?|division|verticals?|business|exports?|international|domestic|india|regions?|geograph\w*|"
    r"subsidiar\w*|standalone|constant currency|dollar|usd|organic|like[- ]for[- ]like|lfl|same[- ]store|"
    r"guid\w*|expect\w*|target\w*|aspir\w*|range|sustain\w*|will|would|should|could|volumes?|"
    r"excluding|ex[- ]\w+|adjusted|one[- ]off|exceptional|per share|eps|gross|contribution|cash|"
    r"advances|deposits|loans?|aum|disburse\w*|annuali[sz]ed|run[- ]rate)\b|\$", re.I)
PERIOD_CTX = re.compile(r"\bh1\b|\bh2\b|first half|half[- ]year|nine months|\b9m\b|year[- ]to[- ]date|\bytd\b|"
                        r"for the (full |financial |fiscal )?year|full[- ]year|financial year|fiscal year|previous year|"
                        r"(?<!Q[1-4] )(?<!Q[1-4])\bfy\s?'?\d\d(?:-\d\d)?\b(?!\s*q)", re.I)
# Once a speaker turns to a segment or subsidiary, later sentences describe that part until they say "consolidated"
SEGMENT_CTX = re.compile(r"\b(segments?|divisions?|verticals?|subsidiar\w*|e-?commerce|o2c|associates?|"
                         r"joint ventures?|jv)\b", re.I)
NAMED_PART = re.compile(r"\b(?!The\b|Our\b|This\b|Overall\b|Total\b|Core\b)[A-Z][\w&-]*\s+(business|portfolio|platform|division)\b")
EXCL_CTX = re.compile(r"exclud|exclusive of|adjusted|one[- ]off|exceptional (?:items?|gains?|loss|charges?)|"
                      r"like[- ]for[- ]like|constant currency", re.I)
WHOLE_CTX = re.compile(r"\b(consolidated|overall|total|company level|group level|the company|company[- ]wide)\b", re.I)
QA_START = re.compile(r"first question (?:is |comes )?(?:from|on the line)|"
                      r"(?:we will|we'll|we shall|let us|let's)? ?now (?:begin|start|open)[^.]{0,40}(?:question|q&a)|"
                      r"open the (?:floor|line) for (?:questions|q&a)", re.I)

PCT = r"(\d{1,3}(?:\.\d{1,2})?)\s*(?:%|percent|per cent)"
UP = r"grew|grow(?:n|ing|th)|increased?|increasing|up\b|rose|higher|expanded|improved|jumped|surged|climbed"
DOWN = r"declined?|declining|down\b|fell|decreased?|lower|degrowth|de-growth|dropped|contracted"
FILLER = r"(?:\s+(?:by|at|of|about|around|approximately|nearly|close to|almost|over|a|an|strong|healthy|robust|solid|" \
         r"impressive|sharp|sharply|significant|significantly|-|–|~))*\s*"
GROWTH_RX = re.compile(rf"(?:\b(?P<v1>{UP}|{DOWN}){FILLER}{PCT})|(?:{PCT}\s*(?:yoy |y-o-y |year[- ]on[- ]year |qoq |q-o-q |sequential )?(?P<v2>{UP}|{DOWN}))", re.I)
# "10% revenue growth, 13.5% EBITDA growth, 14.5% PAT growth"
PRE_RX = re.compile(rf"{PCT}\s+(?P<b>y-?o-?y\s+|year[- ]on[- ]year\s+|q-?o-?q\s+|sequential\s+)?(?P<m>revenues?|sales|top[- ]line|EBITDA|"
                    rf"PAT|net profit|profit after tax|PBT|profit before tax|bottom[- ]line)\s+(?P<v>growth|increase|decline|de-?growth)", re.I)
MARGIN_RX = re.compile(rf"\b(?:EBITDA|operating) margins?\b[^%]{{0,45}}?\b(?:at|of|was|were|to|is|came in at|stood at)\s*"
                       rf"(?:about|around|approximately|roughly|~|close to)?\s*{PCT}", re.I)
AMOUNT_RX = re.compile(r"(?:₹|\brs\.?|\binr)\s?([\d,]+(?:\.\d+)?)\s*(lakh crores?|crores?|cr\b|billion|bn\b|million|mn\b)", re.I)
APPROX_RX = re.compile(r"\b(about|around|approximately|roughly|nearly|close to|almost|over|more than|upwards of)\b|~", re.I)


def _subject_ok(clause, mm):
    """Is the metric in this clause the whole company's?"""
    before = re.split(r"[,:;]", clause[: mm.start()])[-1]
    words = re.findall(r"[\w'&-]+", before.lower())[-5:]
    if any(w not in SUBJECT_OK and not w.isdigit() for w in words):
        return False
    after = clause[mm.end(): mm.end() + 50]
    if re.match(r"\s+(of|from|in|for)\s+(?!the (quarter|period)|operations|₹|inr|rs|about|around|approx|\d|q\d)", after, re.I):
        return False
    return not PART_RX.search(before + clause[mm.start(): mm.end() + 25])


def _date(label):
    m = re.match(r"(\w{3})\w* (\d{4})", label or "")
    return (int(m.group(2)), MONTHS.get(m.group(1)[:3], 0)) if m else None


def quarter_for_call(period, cols):
    """The quarter a call discusses: the latest quarter that ended before the month of the call."""
    d = _date(period)
    if not d:
        return None
    best = None
    for i, c in enumerate(cols):
        qd = _date(c)
        if qd and qd < d:
            best = i
    return best


def _sentences(text):
    text = " ".join(text.split())
    return [s.strip() for s in re.split(r"(?<=[.?!])\s+(?=[A-Z₹“\"(])", text) if s.strip()]


def _clauses(sentence):
    return [c for c in re.split(r";|,\s*(?=while|whereas|with|and|but|translating|resulting|leading)|\bwhile\b|\bwhereas\b", sentence) if c]


def _metric(text, start=0):
    """The first metric named in the text, by position."""
    hits = [(m.start(), key, m) for key, rx in METRIC_RX for m in [rx.search(text, start)] if m]
    if not hits:
        return None, None
    _, key, m = min(hits, key=lambda h: h[0])
    return key, m


def _crore(num, unit):
    v = float(num.replace(",", ""))
    u = unit.lower()
    if u.startswith("lakh"):
        return v * 1e5
    if u.startswith("b"):
        return v * 100
    if u.startswith("m"):
        return v / 10
    return v


def _pct_change(a, b):
    if a is None or b is None or not b:
        return None
    return (a / b - 1) * 100 if b > 0 else None


def extract_claims(pages, is_fin=False):
    """Numeric statements about the quarter from management's prepared remarks."""
    out, prev, in_part = [], [], False
    for pno, page in enumerate(pages, 1):
        text = " ".join(page.split())
        cut = QA_START.search(text)
        stop = bool(cut and (pno > 1 or cut.start() > 1500))
        if stop:
            text = text[: cut.start()]
        for s in _sentences(text):
            ctx = " ".join(prev[-2:])
            last = prev[-1] if prev else ""
            prev.append(s)
            whole = bool(WHOLE_CTX.search(s))
            if whole:
                in_part = False
            elif SEGMENT_CTX.search(s) or NAMED_PART.search(s):
                in_part = True
            if in_part and not whole:
                continue  # "Avenue E-commerce ... sales grew 17%" is the subsidiary's growth, not the company's
            if PERIOD_CTX.search(ctx + " " + s) or EXCL_CTX.search(last + " " + s) or "?" in s or len(s) > 420:
                continue
            for pm in PRE_RX.finditer(s):
                words = re.findall(r"[\w'&-]+", re.split(r"[,:;]", s[: pm.start()])[-1].lower())[-5:]
                if any(w not in SUBJECT_OK for w in words) or PART_RX.search(s[pm.end(): pm.end() + 30]):
                    continue
                key, _ = _metric(pm.group("m"))
                if is_fin and key != "pat":
                    continue
                val = float(pm.group(1)) * (-1 if re.match(r"decl|de-?gr", pm.group("v"), re.I) else 1)
                b = pm.group("b") or ""
                out.append({"metric": key, "kind": "growth", "basis": "qoq" if re.search(r"q|seq", b, re.I) else "yoy",
                            "basis_stated": bool(b), "claimed": val, "approx": False, "quote": s, "page": pno,
                            "anchored": whole})
            for cl in _clauses(s):
                key, mm = _metric(cl)
                if not key or (is_fin and key != "pat") or not _subject_ok(cl, mm):
                    continue
                nxt, nm = _metric(cl, mm.end())
                rest = cl[mm.start(): nm.start() if nm else None]  # stop where the next metric begins
                if re.search(r"guid|range|sustain|target|aspir|expect|medium[- ]term|long[- ]term|constant currency|"
                             r"\bcc\b|exclud|exclusive of|adjusted|one[- ]off|exceptional|like[- ]for[- ]like|"
                             r"from\s+[\d.]+\s*%\s*to", rest, re.I):
                    continue
                mg = MARGIN_RX.search(rest)
                if mg and not is_fin:
                    out.append({"metric": "margin", "kind": "level", "claimed": float(mg.group(1)),
                                "approx": bool(APPROX_RX.search(mg.group(0))), "quote": s, "page": pno,
                                "anchored": whole})
                    continue
                if re.search(r"margin|bps|basis points|from\s+(₹|inr|rs)", rest, re.I):
                    continue
                prev_end = None
                for g in GROWTH_RX.finditer(rest):
                    if prev_end is not None:
                        gap = rest[prev_end: g.start()]
                        if len(gap) > 45 or re.search(r"\b[A-Z][a-z]", gap):
                            break  # "PAT grew 13% and Food Plaza ... grew 28%": the second number is someone else's
                    prev_end = g.end()
                    verb = (g.group("v1") or g.group("v2") or "").lower()
                    val = float(next(x for x in g.groups() if x and x[0].isdigit()))
                    if re.match(DOWN, verb, re.I):
                        val = -val
                    tail = rest[g.end(): g.end() + 25].split("%")[0]  # the words right after this number only
                    if re.search(r"sequential|quarter[- ]on[- ]quarter|q-?o-?q", tail, re.I) or \
                            re.search(r"sequential|quarter[- ]on[- ]quarter|q-?o-?q", rest[max(0, g.start() - 20): g.start()], re.I):
                        basis, stated = "qoq", True
                    else:
                        stated = bool(re.search(r"year[- ]on[- ]year|y-?o-?y|over (the )?(same|corresponding)|last year|a year ago", tail, re.I))
                        basis = "yoy"
                    out.append({"metric": key, "kind": "growth", "basis": basis, "basis_stated": stated,
                                "claimed": val, "approx": bool(APPROX_RX.search(rest[: g.end()])), "quote": s, "page": pno,
                                "anchored": whole})
                a = AMOUNT_RX.search(rest)
                if a and a.start() < 60 and not re.search(r"\bfrom\b|\bto\b|against|compared", rest[: a.start()], re.I):
                    out.append({"metric": key, "kind": "level", "claimed": round(_crore(a.group(1), a.group(2)), 1),
                                "approx": bool(APPROX_RX.search(rest[: a.end()])), "quote": s, "page": pno,
                                "anchored": whole})
        if stop:
            break
    # one row per figure
    seen, uniq = set(), []
    for c in out:
        k = (c["metric"], c["kind"], c.get("basis"), round(c["claimed"], 1))
        if k not in seen:
            seen.add(k)
            uniq.append(c)
    return uniq


def _reported(q, is_fin):
    return {
        "revenue": series(q, "Sales", "Revenue"),
        "ebitda": None if is_fin else series(q, "Operating Profit"),
        "ebitda_oi": None if is_fin else [o + x if o is not None and x is not None else None
                                          for o, x in zip(series(q, "Operating Profit") or [], series(q, "Other Income") or [])],
        "pbt": series(q, "Profit before tax"),
        "pat": series(q, "Net Profit"),
    }


def check_claim(c, rep, i):
    """Recompute the claimed figure for quarter i. Returns (actual, status, tolerance)."""
    if c["metric"] == "margin":
        r, o = rep["revenue"], rep["ebitda_oi" if c.get("oi") else "ebitda"]
        if not r or not o or r[i] is None or o[i] is None or not r[i]:
            return None, None
        actual = o[i] / r[i] * 100
        tol = 1.0 if not c["approx"] else 1.3
    elif c["kind"] == "level":
        s = rep.get(c["metric"])
        if not s or s[i] is None:
            return None, None
        actual = s[i]
        if not actual or not 0.8 <= c["claimed"] / actual <= 1.25:
            return None, None  # an annual, half-year or segment figure; cannot be checked against the quarter
        tol = max(abs(actual) * (0.03 if c["approx"] else 0.015), 1)
    else:
        s = rep.get(c["metric"])
        j = i - 4 if c["basis"] == "yoy" else i - 1
        if not s or j < 0 or s[i] is None or s[j] is None:
            return None, None
        actual = _pct_change(s[i], s[j])
        if actual is None:
            return None, None
        tol = max(1.5 if not c["approx"] else 2.5, abs(c["claimed"]) * 0.1)
    diff = abs(actual - c["claimed"])
    status = "matches" if diff <= tol else ("close" if diff <= 2 * tol else "differs")
    return actual, status


def _fmt_amount(v):
    return f"₹{v:,.0f} cr"


def _sentence_for(c, actual, qlabel):
    m = LABEL[c["metric"]]
    if c["metric"] == "margin":
        return f"{m} {c['claimed']:g}%", f"{actual:.1f}%"
    if c["kind"] == "level":
        return f"{m} {_fmt_amount(c['claimed'])}", _fmt_amount(actual)
    base = "on a year ago" if c["basis"] == "yoy" else "on the last quarter"
    word = "up" if c["claimed"] >= 0 else "down"
    aw = "up" if actual >= 0 else "down"
    return f"{m} {word} {abs(c['claimed']):g}% {base}", f"{aw} {abs(actual):.1f}%"


def _best(c, reps, i):
    """Try the statements the company could mean: consolidated, then standalone; for an unstated basis, a year ago then
    the last quarter. Returns the closest reading so a mismatch is never down to FINN picking the wrong one."""
    tries = []
    for name, rep in reps:
        variants = [c]
        if c["kind"] == "growth" and not c["basis_stated"]:
            variants.append(dict(c, basis="qoq"))
        if c["metric"] == "ebitda":  # many companies count other income in EBITDA
            variants += [dict(v, metric="ebitda_oi") for v in list(variants)]
        if c["metric"] == "margin":
            variants.append(dict(c, oi=True))
        for v in variants:
            actual, status = check_claim(v, rep, i)
            if actual is not None:
                label = name + (", EBITDA incl. other income" if v["metric"] == "ebitda_oi" or v.get("oi") else "")
                tries.append((["matches", "close", "differs"].index(status), abs(actual - v["claimed"]), label,
                              dict(v, metric=c["metric"]), actual, status))
    if not tries:
        return None
    tries.sort(key=lambda t: (t[0], t[1]))
    _, _, name, v, actual, status = tries[0]
    return name, v, actual, status


def claims_check(scr, is_fin, max_calls=2):
    q = scr.get("quarters")
    calls = [c for c in scr["docs"]["concalls"] if c.get("transcript")][:max_calls]
    if not q or not calls:
        return {"items": [], "checked": 0, "summary": "No earnings call transcript on file to check against."}
    reps = [(scr.get("basis", "consolidated"), _reported(q, is_fin), scr["url"], q["cols"])]
    if scr.get("basis") == "consolidated":
        alt = screener(scr["symbol"], variants=("",))
        if alt and alt.get("quarters"):
            reps.append(("standalone", _reported(alt["quarters"], is_fin), alt["url"], alt["quarters"]["cols"]))
    items = []
    for call in calls:
        try:
            # parsed once at 40 pages and shared with the guidance quotes; only the prepared remarks are checked
            pages = pdf_pages(call["transcript"], max_pages=40)[:14]
        except Exception:
            continue
        claims = extract_claims(pages, is_fin)
        # a rupee figure nowhere near the quarter's means the sentence is about something else (a segment, a year)
        off_scope = set()
        for c in claims:
            if c["kind"] == "level" and c["metric"] != "margin":
                ok = False
                for _, rep, _, cols in reps:
                    i = quarter_for_call(call["period"], cols)
                    vals = [rep.get(m) for m in (c["metric"], "ebitda_oi" if c["metric"] == "ebitda" else c["metric"])]
                    ok = ok or any(v and i is not None and v[i] and 0.8 <= c["claimed"] / v[i] <= 1.25 for v in vals)
                if not ok:
                    off_scope.add(c["quote"])
        claims = [c for c in claims if c["quote"] not in off_scope]
        found_rows = []
        for c in claims:
            # each statement set is indexed by its own quarter columns
            best = None
            for name, rep, url, cols in reps:
                i = quarter_for_call(call["period"], cols)
                if i is None:
                    continue
                r = _best(c, [(name, rep)], i)
                if r and (best is None or ["matches", "close", "differs"].index(r[3]) < ["matches", "close", "differs"].index(best[0][3])):
                    best = (r, url, cols[i])
            if not best:
                continue
            (name, v, actual, status), url, qlabel = best
            said, found = _sentence_for(v, actual, qlabel)
            found_rows.append({
                "call": call["period"], "quarter": qlabel, "metric": v["metric"], "kind": v["kind"],
                "basis": v.get("basis"), "statements": name,
                "claimed": v["claimed"], "actual": round(actual, 1), "status": status,
                "said": said, "found": found, "quote": v["quote"][:320], "page": v["page"],
                "source": call["transcript"], "actual_source": url + "#quarters", "_anchored": v.get("anchored"),
            })
        # A sentence whose rupee figure matches the quarter is clearly about the whole company
        anchored_quotes = {r["quote"] for r in found_rows if r["kind"] == "level" and r["metric"] != "margin" and r["status"] == "matches"}
        for r in found_rows:
            anchored = r.pop("_anchored") or r["quote"] in anchored_quotes
            if r["status"] != "matches" and not anchored:
                continue  # could be a segment or an adjusted figure; not enough to say management misstated
            items.append(r)
    n = len(items)
    ok = sum(1 for x in items if x["status"] == "matches")
    off = [x for x in items if x["status"] == "differs"]
    if not n:
        summary = "The prepared remarks on the latest calls had no whole-company figures to check; management spoke in segment numbers."
    elif not off:
        summary = (f"The one company-wide figure management read out on the last {len(calls)} calls matches the reported results."
                   if n == 1 else f"All {n} company-wide figures management read out on the last {len(calls)} calls match the reported results.")
    else:
        summary = f"{ok} of {n} company-wide figures read out on the last {len(calls)} calls match the reported results; {len(off)} do not."
    return {"items": items, "summary": summary, "checked": n, "matches": ok, "differs": len(off),
            "note": "Each figure management read out on the call is recomputed from the quarterly results the company "
                    "filed, consolidated and standalone, with EBITDA both before and after other income. A figure is only "
                    "called a mismatch when the call clearly speaks about the whole company."}


# ------------------------------------------------------------------ profit quality

def _moved(v):
    return f"grew {v:.0f}%" if v >= 0.5 else f"fell {abs(v):.0f}%" if v <= -0.5 else "was flat"


def _check(key, title, status, text, src, value=None):
    return {"key": key, "title": title, "status": status, "text": text, "src": src, "value": value}


def _aligned(tbl_a, label_a, tbl_b, label_b):
    """Values of two annual rows on the years both tables share."""
    if not tbl_a or not tbl_b:
        return [], [], []
    a, b = series(tbl_a, *label_a), series(tbl_b, *label_b)
    if not a or not b:
        return [], [], []
    ia = {c: v for c, v in zip(tbl_a["cols"], a) if c != "TTM"}
    ib = {c: v for c, v in zip(tbl_b["cols"], b) if c != "TTM"}
    years = [y for y in tbl_a["cols"] if y in ia and y in ib and ia[y] is not None and ib[y] is not None]
    return years, [ia[y] for y in years], [ib[y] for y in years]


def profit_quality(scr, is_fin):
    pl, cf, bs, rt, q, hold = scr["pl"], scr.get("cf"), scr.get("bs"), scr.get("ratios"), scr.get("quarters"), scr.get("holding")
    u = scr["url"]
    checks = []

    # 1. Does profit turn into cash?
    if not is_fin:
        yrs, cfo, pat = _aligned(cf, ("Cash from Operating",), pl, ("Net Profit",))
        if len(yrs) >= 3 and sum(pat[-3:]) > 0:
            r = sum(cfo[-3:]) / sum(pat[-3:]) * 100
            st = "ok" if r >= 85 else "watch" if r >= 60 else "flag"
            msg = {"ok": f"Over the last 3 years operating cash flow was {r:.0f}% of reported profit, so profit is turning into cash.",
                   "watch": f"Over the last 3 years only {r:.0f}% of reported profit came in as operating cash. Some profit is tied up in inventory or receivables.",
                   "flag": f"Over the last 3 years only {r:.0f}% of reported profit came in as operating cash. Reported profit is running well ahead of cash."}[st]
            checks.append(_check("cash", "Profit turns into cash", st, msg, u + "#cash-flow", round(r)))

    # 2. Are customers paying slower?
    dd = series(rt, "Debtor Days") if rt else None
    if dd and not is_fin:
        v = [x for x in dd if x is not None]
        if len(v) >= 4:
            now, then = v[-1], v[-4]
            d = now - then
            st = "flag" if d >= 15 and now > then * 1.25 else "watch" if d >= 7 else "ok"
            msg = (f"Customers now take {now:.0f} days to pay, against {then:.0f} three years ago."
                   + (" Sales may be booked faster than cash is collected." if st != "ok" else ""))
            checks.append(_check("debtors", "Customers pay on time", st, msg, u + "#ratios", round(now)))

    # 3. How much of profit is other income? (Not for lenders, whose fee income is core business.)
    oi, pbt = series(pl, "Other Income"), series(pl, "Profit before tax")
    if not is_fin and oi and pbt and pbt[-1] and pbt[-1] > 0 and oi[-1] is not None:
        r = oi[-1] / pbt[-1] * 100
        per = "the last 12 months" if pl["cols"][-1] == "TTM" else pl["cols"][-1]
        st = "flag" if r >= 25 else "watch" if r >= 15 else "ok"
        msg = (f"Other income (interest, investment gains, one-offs) was {r:.0f}% of pre-tax profit in {per}."
               + {"ok": " The core business earns most of the profit.",
                  "watch": " A meaningful slice of profit does not come from the core business.",
                  "flag": " A large part of profit does not come from the core business."}[st])
        checks.append(_check("other_income", "Profit comes from the core business", st, msg, u + "#profit-loss", round(r)))

    # 4. Did profit grow faster than the operations behind it?
    op, pat = series(pl, "Operating Profit", "Financing Profit"), series(pl, "Net Profit")
    yrs = [c for c in pl["cols"] if c != "TTM"]
    if op and pat and len(yrs) >= 2:
        n = len(yrs)
        g_op, g_pat = _pct_change(op[n - 1], op[n - 2]), _pct_change(pat[n - 1], pat[n - 2])
        if g_op is not None and g_pat is not None:
            gap = g_pat - g_op
            st = "watch" if gap >= 15 and g_pat > 0 else "ok"
            what = "financing profit" if is_fin else "operating profit"
            msg = (f"In FY{yrs[-1][-2:]}, net profit {_moved(g_pat)} while {what} {_moved(g_op)}."
                   + (" The gap came from below the operating line: other income, interest, depreciation or tax." if st != "ok" else ""))
            checks.append(_check("growth_gap", "Profit growth backed by operations", st, msg, u + "#profit-loss", round(gap)))

    # 5. Is the latest quarter's tax rate unusual?
    if q:
        tax = series(q, "Tax %")
        v = [x for x in (tax or []) if x is not None]
        if len(v) >= 5:
            now, base = v[-1], sum(v[-5:-1]) / 4
            st = "watch" if abs(now - base) >= 8 else "ok"
            msg = (f"Tax rate in {q['cols'][-1]} was {now:.0f}%, against {base:.0f}% on average over the four quarters before."
                   + (" A lower rate flatters profit; a higher one depresses it. Check the notes for why." if st != "ok" else ""))
            checks.append(_check("tax", "Tax rate is normal", st, msg, u + "#quarters", round(now)))

    # 6. A one-off in the latest quarter?
    if q and not is_fin:
        qoi, qpbt = series(q, "Other Income"), series(q, "Profit before tax")
        if qoi and qpbt and len(qoi) >= 5 and qpbt[-1] and qpbt[-1] > 0 and qoi[-1] is not None:
            prior = [x for x in qoi[-5:-1] if x is not None]
            avg = sum(prior) / len(prior) if prior else 0
            share = qoi[-1] / qpbt[-1] * 100
            spike = avg > 0 and qoi[-1] > 2 * avg and share >= 10
            st = "watch" if spike else "ok"
            msg = (f"Other income in {q['cols'][-1]} was ₹{qoi[-1]:,.0f} cr, {qoi[-1] / avg:.1f}× its recent average and {share:.0f}% of pre-tax profit. "
                   "Part of this quarter's profit may not repeat." if spike else
                   f"No unusual jump in other income in {q['cols'][-1]}.")
            checks.append(_check("one_off", "No one-off boost this quarter", st, msg, u + "#quarters", round(share)))

    # 7. Debt
    if not is_fin and bs:
        debt = series(bs, "Borrowings", "Borrowing")
        eq = [a + b if a is not None and b is not None else None
              for a, b in zip(series(bs, "Equity Capital") or [], series(bs, "Reserves") or [])]
        pairs = [(d, e) for d, e in zip(debt or [], eq) if d is not None and e]
        if len(pairs) >= 3:
            de_now, de_then = pairs[-1][0] / pairs[-1][1], pairs[-3][0] / pairs[-3][1]
            st = "flag" if de_now >= 1 and de_now > de_then + 0.2 else "watch" if de_now >= 0.5 and de_now > de_then + 0.15 else "ok"
            msg = (f"Debt is {de_now:.2f}× equity, against {de_then:.2f}× two years earlier."
                   + (" Borrowing is rising faster than the company's own capital." if st != "ok" else ""))
            checks.append(_check("debt", "Debt under control", st, msg, u + "#balance-sheet", round(de_now, 2)))

    # 8. Promoters
    prom = series(hold, "Promoters") if hold else None
    v = [x for x in (prom or []) if x is not None]
    if len(v) >= 5:
        d = v[-1] - v[-5]
        st = "flag" if d <= -5 else "watch" if d <= -2 else "ok"
        msg = (f"Promoters hold {v[-1]:.1f}%, " + (f"down {abs(d):.1f} points" if d < -0.05 else f"up {d:.1f} points" if d > 0.05 else "unchanged")
               + f" over the last {len(v[-5:]) - 1} reported quarters.")
        checks.append(_check("promoter", "Promoters are not selling", st, msg, u + "#shareholding", round(v[-1], 1)))

    bad = [c for c in checks if c["status"] != "ok"]
    if not checks:
        summary = "Not enough history to run the profit-quality checks."
    elif not bad:
        summary = "The one check that applies is clean." if len(checks) == 1 else f"All {len(checks)} checks are clean."
    else:
        summary = f"{len(checks) - len(bad)} of {len(checks)} checks are clean; look at: " + ", ".join(c["title"].lower() for c in bad) + "."
    return {"checks": checks, "summary": summary, "clean": len(checks) - len(bad), "total": len(checks)}
