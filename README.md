# FINN

A mobile-first investing agent for one investor's portfolio. It opens on a triaged feed of what changed across 18 NSE holdings, and one tap takes any company into a research view built only from the company's own filings.

Three screens, nothing else:

| Screen | What it does |
|---|---|
| **Today** (push) | "2 things need you", then what's worth a look, then routine filings set aside, then the day's headlines, then your holdings with live prices. Pick today, this week or this month. Each item is one plain sentence plus the reasons for its rank. |
| **Research** (pull) | Any NSE ticker. Live price, headline numbers, **what they said vs what they reported** (every figure read out on the earnings call, recomputed from the filed results), **promised vs delivered** (guidance checked against later results), **is the profit real?** (eight checks on cash, receivables, other income, tax, debt, promoters), trajectory, cash quality, balance sheet, bull vs bear. Every line links to its source. |
| **Alerts** | The investor sets what "unusual" means: price move %, volume multiple, the alert line, and which filing types they care about. Today updates instantly. |

Opening a card shows the detail: the results table (Revenue, EBITDA, PBT, PAT vs last quarter and last year), the incoming person's background for a management change, the size of an order or legal claim as a share of revenue, the market's reaction with a 60-day chart, and **why it ranks here**: the score broken into its factors.

## Run it

```bash
pip install -r requirements.txt
python serve.py            # http://localhost:8000 — open it on any phone or computer
```

The server keeps the data live by itself, so the app opens instantly with fresh data:

| What | How often | Source |
|---|---|---|
| Price, change and today's intraday line for every holding and the Nifty | every 15 seconds in market hours (one batched request), every 5 minutes when closed | Yahoo Finance |
| Filings, re-scored and re-ranked, with business headlines | every 2 minutes on trading days until 11 pm, when results land; every 10 minutes overnight and at weekends | NSE announcements, Google News RSS |
| Research for any NSE ticker | when you ask. A company with a saved copy under 36 hours old opens at once and gets today's price and headlines; any other is built on the spot (about 20 seconds on the free server, then remembered for 10 minutes); Refresh always rebuilds. Its price joins the 15-second loop while you look at it | screener.in, annual reports, concall transcripts |

While it is open, the app asks the server every 30 seconds for one small update (`/api/live`): new prices are written into the page in place, with a brief green or red flash on any price that moved, and the page only downloads the feed again when a new filing has changed it. The top bar shows **Live · checked 12s ago · next in 18s**, ticking every second, or **Market closed** with the last trade time, plus a Refresh button. Updates pause while the tab is hidden and resume the moment you come back. After a cold start the server serves the last saved snapshot (`data/feed.json`) until its first refresh of the filings finishes; until then the top bar says **Waking up** and the page says which time the filings are from. Under the Today headline the page always says when filings and headlines were last checked. If NSE stops answering, the server keeps the last good feed rather than showing an empty one, and that line keeps the true time.

A refresh is cheap on purpose, because Render's free plan has a tenth of a CPU: each company page is parsed once and kept for 30 minutes, parsed PDFs are kept on disk, and a source that just failed is not retried for 5 minutes (its last good copy is used instead). A steady refresh takes about 0.2 seconds of CPU.

Without a server (any static host), the app opens the saved snapshot and says "Saved" with its date instead of "Live".

### Put it online (free)

1. Push this folder to a GitHub repository.
2. On [render.com](https://render.com), choose **New → Blueprint** and pick the repository. `render.yaml` sets everything up. You get an address like `https://finn.onrender.com` that works on any phone or computer.
3. Optional: add `ANTHROPIC_API_KEY` in Render's environment settings to grade management's promises for any ticker typed in, not only the holdings that are already graded.

Render's free plan sleeps after 15 idle minutes and takes about 30 seconds to wake. The saved snapshot covers that gap.

Batch mode, for a static host refreshed on a schedule: `python -m pipeline.build --days 30` (the GitHub Action in `.github/workflows/refresh.yml` does this daily).

## How it works

```
NSE announcements ─┐                          ┌─> data/feed.json ──────┐
Yahoo prices ──────┼─> pipeline/push.py ──────┤                        ├─> app (static, phone-first)
Google News RSS ───┤   pipeline/news.py       │                        │
screener.in tables ┤                          └─> data/research/*.json ┘
Concall transcripts┴─> pipeline/research.py + reality.py
```

The app only reads JSON. A slow or blocked source can never break it.

**Push.** Each filing is classified by rules on the exchange's own subject line (17 categories from the brief's reference, plus subtypes such as "CFO resigns" or "order win"). Material items are enriched by reading the filing PDF: the board's results numbers, the brief profile SEBI requires in appointment letters, the authority and amount in a legal order. A results day produces four to six filings (outcome, press release, deck, newspaper ad, call recording); FINN folds them into one card. A price move is joined to the filing that explains it; a move with no filing becomes its own card, which is often the most interesting thing in the feed.

**Priority** is a transparent formula, not a model's opinion:

```
score = base(category, role) × size vs company × surprise vs trend × position weight + market reaction + busy week
```

- Size is relative: an L&T order is read from its disclosed band (L&T reports "significant / large / major / mega" instead of a number) and shown as a % of revenue.
- Surprise compares this quarter's PAT growth with the last four quarters' trend. In-line results are scored down, surprises up.
- A filing after the 15:30 close is matched with the next session's move.
- The top factors become the reason chips on each card; the detail shows the full breakdown.

On the live data (252 filings over 30 days) this gives about 2 to 5 "act now" items a week, 10 to 25 "worth a look", and the rest folded into a routine count.

**Pull.** Numbers are computed in code from screener.in's standardised tables, which are built from annual reports and quarterly filings: growth rates, margins, cash conversion (operating cash flow ÷ PAT), debt to equity, working-capital and debtor days, promoter holding. Bull and bear points are fixed rules over those numbers, so the same data always gives the same case. Guidance comes from concall transcripts: a regex pre-filter keeps only forward-looking sentences with a number in them, a cheap model turns those into rows (metric, promise, horizon, quote, page), and a stronger model grades each against the later reported figure: kept, on track, partly, missed, moved, pending.

**Said vs reported.** `pipeline/reality.py` reads management's prepared remarks on the last two earnings calls (it stops where the Q&A begins) and pulls out every company-wide figure: "revenue grew 11.2% year on year", "EBITDA margin was 14.4%", "profit after tax of ₹9,900 million". Each is recomputed from the quarterly results the company filed, consolidated and standalone, with EBITDA before and after other income, and marked matches, close or doesn't match. Sentences about a segment, a subsidiary, a half-year, constant currency or an adjusted figure are left out, because they cannot be checked against the quarter. A figure is only called a mismatch when the call clearly speaks about the whole company. On the 22 companies in this repo it finds 1 to 4 checkable figures for 11 of them, and all 29 match; the other companies talk mostly in segment numbers.

**Is the profit real?** Eight rules over the statements: 3-year operating cash flow vs profit, days to get paid vs three years ago, other income as a share of pre-tax profit, net profit growth vs operating profit growth, this quarter's tax rate vs the last four, an other-income spike this quarter, debt to equity vs two years ago, promoter holding over four quarters. Each comes back clean, watch or flag, in one sentence, with a link. Titan, for example, is flagged because only 57% of three years' profit arrived as cash (gold inventory) and this quarter's other income was 6.6 times normal.

**Headlines** come from Google News RSS per company, filtered so the headline names the company itself (not "Reliance Power" for Reliance, not "Stoke-on-Trent" for Trent). They are shown for context and never feed a score or the research.

## Model routing and cost

| Step | Tool | Cost |
|---|---|---|
| Classify filings | Rules on the exchange subject line | Free |
| Card text | The exchange's own one-liner and summary; the first lines of the filing letter | Free |
| Results numbers, ratios, growth, bull/bear | Code over standardised tables | Free |
| Said vs reported, profit quality | Regex over the transcript, code over the statements | Free |
| Headlines | Google News RSS | Free |
| Bio, legal order, deal size | Regex over the filing PDF; optional cheap model to compress the bio | ~0 |
| Guidance extraction | Regex pre-filter, then a small model on ~15 sentences per call | ~4k tokens per transcript |
| Promised vs delivered verdicts | One stronger-model call per company per quarter | ~3k tokens per company |
| Everything | Disk cache by input hash and per ticker per quarter | Re-runs are free |

The daily push run uses no model at all. Set `ANTHROPIC_API_KEY` to turn on the two model steps; `pipeline/llm.py` logs calls, tokens and cache hits per run. In this repo the guidance for 16 of the 18 holdings was extracted and graded during the build by Claude reading the same transcript text, and is cached in `data/guidance/` with the page of each promise and of the result it was checked against (or the filed quarterly results, where no later call is on file). The other two have nothing to grade: ITC has no call transcripts on file, and CDSL's calls contain no numeric promises. Several companies (TCS, Airtel, Eternal) give little numeric guidance, so they have only one or two rows; a promise that is not due yet is shown but not counted. Tickers outside the portfolio show the forward-looking quotes FINN found, unverified, until the model step runs.

## Deliberately left out

- Intraday prices and real phone push notifications. The badge on Today stands in; a PWA notification is the next step.
- User accounts. Preferences live in the browser.
- LinkedIn lookups for incoming executives. The SEBI-mandated profile in the filing is primary, free and already on file.
- Four of the eight research blocks (narrative, full guidance table for all companies, segment deep-dive, a chat box). Promised vs delivered is the one the brief values most, so it got the depth.

## Next

- Run the model step every quarter so the graded promises stay current as new calls come in.
- Hand-label 150 recent filings as "matters / doesn't" and report precision of Act now against them.
- Learn from "less like this": nudge category weights per investor instead of only muting.
- A 60-second morning audio brief of the Act now items.
