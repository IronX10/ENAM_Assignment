/* Delta app. Talks to the Delta server for live data (api/live every 30 s, api/feed when it changes, api/research);
   if no server is reachable it opens the last saved snapshot in data/ and says so. */
(function () {
  "use strict";

  const $ = (s, el = document) => el.querySelector(s);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const store = {
    get(k, d) { try { const v = localStorage.getItem("finn:" + k); return v ? JSON.parse(v) : d; } catch (e) { return d; } },
    set(k, v) { try { localStorage.setItem("finn:" + k, JSON.stringify(v)); } catch (e) { /* private mode */ } },
  };

  const CAT = {
    results: "Results", "M&A": "Deal", fund_raise: "Fund raise", litigation: "Legal", insider_trading: "Insider trade",
    earnings_call: "Earnings call", investor_meetings: "Investor meeting", credit_rating: "Credit rating", dividend: "Dividend",
    bonus_split: "Bonus or split", capex: "Capex", press_release: "Update", postal_ballot: "Postal ballot", agm_egm: "AGM",
    pledging: "Promoter pledge", unclassified: "Routine", management_change: "Management", price: "Price move",
  };
  const CARE = ["results", "management_change", "M&A", "fund_raise", "litigation", "credit_rating", "pledging", "insider_trading", "capex", "press_release", "dividend", "earnings_call", "investor_meetings", "agm_egm", "unclassified"];
  const POLL = 30e3; // the app asks the server for fresh prices every 30 seconds

  const S = {
    feed: null, live: false, feedLive: true, quotes: {}, quotesAt: null, checkedAt: null, marketOpen: false, index: [], nextAt: 0, every: null, rbusy: new Set(),
    tab: "today", days: store.get("days", 7), seen: new Set(store.get("seen", [])), less: new Set(store.get("less", [])),
    rules: null, routineOpen: false, newsOpen: false, research: {}, rsym: store.get("rsym", "POLYCAB"), rerr: null, loading: false, open: null,
  };

  // ---------------------------------------------------------------- helpers
  // Exchange times are IST. Times without an offset are read as IST, and every time is shown in IST,
  // so the app tells the same story on a phone in Mumbai or a laptop in London.
  const D = (iso) => iso instanceof Date ? iso : /^\d{4}-\d\d-\d\d$/.test(iso) ? new Date(iso + "T12:00:00+05:30")
    : new Date(/(Z|[+-]\d\d:?\d\d)$/.test(iso) ? iso : iso + "+05:30");
  const IST = { timeZone: "Asia/Kolkata" };
  const now = () => D(S.feed.generated);
  const pct = (v, d = 1) => v == null || isNaN(v) ? "–" : (v > 0 ? "+" : v < 0 ? "−" : "") + Math.abs(v).toFixed(d) + "%";
  const cls = (v) => v == null ? "" : v > 0 ? "up" : v < 0 ? "down" : "";
  const inr = (v, d = 0) => v == null ? "–" : v.toLocaleString("en-IN", { minimumFractionDigits: d, maximumFractionDigits: d });
  const hm = (iso) => D(iso).toLocaleTimeString("en-IN", { ...IST, hour: "2-digit", minute: "2-digit" });
  const dm = (iso) => D(iso).toLocaleDateString("en-IN", { ...IST, day: "numeric", month: "short" });
  const dayHm = (iso) => (dm(iso) === dm(new Date()) ? "" : D(iso).toLocaleDateString("en-IN", { ...IST, weekday: "short" }) + " ") + hm(iso);
  function ago(iso) {
    const h = (now() - D(iso)) / 36e5;
    if (h < 1) return "now";
    if (h < 24) return Math.round(h) + "h";
    const d = Math.round(h / 24);
    return d < 7 ? d + "d" : dm(iso);
  }
  const holding = (sym) => S.feed.portfolio.find((p) => p.symbol === sym) || { symbol: sym, name: sym, weight: 3 };
  function bucket(e) {
    if (S.rules.muted_categories.includes(e.category) || S.less.has(e.symbol + ":" + e.category)) return "rt";
    return e.score >= S.rules.alert_score ? "act" : e.score >= S.rules.digest_score ? "look" : "rt";
  }
  function move(sym) {
    const q = S.quotes[sym];
    if (q && q.chg != null) return q.chg;
    const p = S.feed.prices[sym];
    return p && p.c.length > 1 ? (p.c[p.c.length - 1] / p.c[p.c.length - 2] - 1) * 100 : null;
  }
  function toast(msg) {
    const t = document.createElement("div");
    t.className = "toast"; t.setAttribute("role", "status"); t.textContent = msg;
    document.body.appendChild(t); setTimeout(() => t.remove(), 2200);
  }

  // ---------------------------------------------------------------- price moves, by the investor's own thresholds
  function priceItems() {
    const out = [], since = new Date(now() - S.days * 864e5 + 19800e3).toISOString().slice(0, 10);
    const nf = S.feed.prices.NIFTY, nIdx = nf ? Object.fromEntries(nf.d.map((d, i) => [d, nf.c[i]])) : {};
    for (const h of S.feed.portfolio) {
      const px = S.feed.prices[h.symbol]; if (!px) continue;
      for (let i = 21; i < px.c.length; i++) {
        const d = px.d[i]; if (d < since) continue;
        const ret = (px.c[i] / px.c[i - 1] - 1) * 100;
        const vx = px.v[i] / (px.v.slice(i - 20, i).reduce((a, b) => a + b, 0) / 20 || 1);
        if (Math.abs(ret) < S.rules.price_move_pct && vx < S.rules.volume_multiple) continue;
        // a filing from the previous session up to this close already carries the move on its own item
        const from = px.d[i - 1] + "T00:00:00", to = d + "T15:30:00";
        if (S.feed.events.some((e) => e.symbol === h.symbol && e.score >= S.rules.digest_score && e.time >= from && e.time <= to)) continue;
        const rel = nIdx[d] && nIdx[px.d[i - 1]] ? ret - (nIdx[d] / nIdx[px.d[i - 1]] - 1) * 100 : null;
        const unexplained = Math.abs(ret) >= 2 ? 10 : 0;
        const base = 22 + Math.min(30, Math.abs(ret) * 4) + Math.min(18, (vx - 1) * 6);
        const pos = +(0.75 + 0.04 * h.weight).toFixed(2);
        const why = [`volume ${vx.toFixed(1)}× normal`];
        if (rel != null && Math.abs(rel) >= 2) why.push(`${pct(rel)} vs Nifty`);
        if (unexplained) why.push("no filing explains it");
        out.push({
          id: `px-${h.symbol}-${d}`, symbol: h.symbol, name: h.name, category: "price", time: d + "T15:30:00",
          score: Math.round(Math.min(100, base * pos + unexplained)), why: why.join(" · "), px: { ret, vx, rel, date: d },
          so_what: unexplained ? `${ret >= 0 ? "Up" : "Down"} ${Math.abs(ret).toFixed(1)}% with no news from the company` : `Heavy trading, price barely moved (${pct(ret)})`,
          factors: { base: Math.round(base), position: pos, unexplained },
        });
      }
    }
    return out;
  }

  function lists() {
    const since = new Date(now() - S.days * 864e5);
    const g = { act: [], look: [], rt: [] };
    for (const e of S.feed.events.filter((x) => D(x.time) >= since).concat(priceItems())) { e._b = bucket(e); g[e._b].push(e); }
    const order = (a, b) => (S.seen.has(a.id) - S.seen.has(b.id)) || b.score - a.score;
    g.act.sort(order); g.look.sort(order); g.rt.sort((a, b) => (a.time < b.time ? 1 : -1));
    return g;
  }
  const tidy = (t) => { t = String(t || "").replace(/^[\s,;:.\-–]+/, ""); return t.charAt(0).toUpperCase() + t.slice(1); };
  const whyLine = (e) => e.why || (e.reasons || []).join(" · ").replace(/(^|· )([A-Z])/g, (m, a, b) => a + (a ? b.toLowerCase() : b));
  const allItems = () => S.feed.events.concat(priceItems());

  // ---------------------------------------------------------------- status line
  // S.live: a Delta server is answering. S.feedLive: that server has finished its first try at refreshing the filings.
  // For the first couple of minutes after the server wakes it serves live prices with the filings it was saved with.
  const statusClass = () => "status" + (S.live && S.feedLive ? " live" + (S.marketOpen ? " open" : "") : "");
  function statusLine() {
    if (!S.live) return `<span class="status"><i></i>Saved ${dm(S.feed.generated)}, ${hm(S.feed.generated)}</span>`;
    return `<span class="${statusClass()}"><i></i><span id="tick">${tickText()}</span> <button data-refresh aria-label="Refresh now">Refresh</button></span>`;
  }
  function tickText() {
    if (!S.live) return "";
    if (!S.feedLive) return "Waking up · filings refreshing";
    const secs = (iso) => Math.max(0, Math.round((Date.now() - D(iso)) / 1e3));
    const age = (n) => n < 60 ? n + "s ago" : n < 3600 ? Math.round(n / 60) + "m ago" : hm(new Date(Date.now() - n * 1e3));
    if (!S.marketOpen) return `Market closed${S.quotesAt ? " · last trade " + dayHm(S.quotesAt) : ""}`;
    const next = Math.max(0, Math.ceil((S.nextAt - Date.now()) / 1e3));
    return `Live · checked ${S.checkedAt ? age(secs(S.checkedAt)) : "now"}${next ? ` · next in ${next}s` : ""}`;
  }
  // when the filings and headlines on screen were last fetched from the exchange
  function filingsStamp() {
    if (!S.live) return "";
    const t = S.feed.filings_checked || S.feed.generated;
    const when = (dm(t) === dm(new Date()) ? "" : dm(t) + ", ") + hm(t);
    return `<p class="stamp" id="stamp">${S.feedLive ? `Filings and headlines checked ${when}.` : `Filings as of ${when}. Newer ones are loading.`}</p>`;
  }

  // ---------------------------------------------------------------- live prices, patched in place every 30 seconds
  function quoteOf(sym) {
    const q = S.quotes[sym];
    if (q) return q;
    const p = S.feed.prices[sym];
    return p ? { price: p.c[p.c.length - 1], chg: move(sym), day: null } : null;
  }
  function daySpark(day, prev) {
    if (!day || day.length < 2) return "";
    const W = 56, H = 18, lo = Math.min(...day, prev || Infinity), hi = Math.max(...day, prev || -Infinity);
    const x = (i) => (i / (day.length - 1)) * W, y = (v) => 1 + (1 - (v - lo) / (hi - lo || 1)) * (H - 2);
    const up = day[day.length - 1] >= (prev || day[0]);
    return `<svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" aria-hidden="true">${prev ? `<line x1="0" x2="${W}" y1="${y(prev).toFixed(1)}" y2="${y(prev).toFixed(1)}" class="pc"/>` : ""}
      <polyline points="${day.map((v, i) => x(i).toFixed(1) + "," + y(v).toFixed(1)).join(" ")}" class="${up ? "su" : "sd"}"/></svg>`;
  }
  const priceHTML = (q) => q && q.price != null ? "₹" + inr(q.price, 2) : "–";
  function niftyLine() {
    const q = S.quotes.NIFTY || (S.feed.prices.NIFTY ? { price: S.feed.prices.NIFTY.c.slice(-1)[0], chg: move("NIFTY") } : null);
    if (!q) return "";
    return `<p class="index"><span>Nifty 50</span> <b class="n" data-q="NIFTY" data-f="plain">${inr(q.price, 2)}</b> <span class="n ${cls(q.chg)}" data-q="NIFTY" data-f="chg">${pct(q.chg, 2)}</span></p>`;
  }
  // update every live number on screen without rebuilding the page, and flash the ones that moved
  function patchPrices() {
    document.querySelectorAll("[data-q]").forEach((el) => {
      const q = el.dataset.q === "NIFTY" ? S.quotes.NIFTY : quoteOf(el.dataset.q);
      if (!q) return;
      const f = el.dataset.f;
      if (f === "spark") { el.innerHTML = daySpark(q.day, q.prev); return; }
      const txt = f === "chg" ? pct(q.chg, 2) : f === "plain" ? inr(q.price, 2) : priceHTML(q);
      if (el.textContent === txt) return;
      const before = parseFloat((el.textContent || "").replace(/[^0-9.−-]/g, "").replace("−", "-"));
      el.textContent = txt;
      if (f === "chg") { el.classList.remove("up", "down"); const c = cls(q.chg); if (c) el.classList.add(c); return; }
      const now = q.price;
      if (!isNaN(before) && before !== now) {
        el.classList.remove("flash-up", "flash-down"); void el.offsetWidth;
        el.classList.add(now > before ? "flash-up" : "flash-down");
      }
    });
    const t = $("#tick"); if (t) { t.textContent = tickText(); t.parentElement.className = statusClass(); }
  }

  // ---------------------------------------------------------------- Today
  function renderToday() {
    const g = lists();
    const unread = g.act.filter((e) => !S.seen.has(e.id)).length;
    const title = unread ? `${unread} ${unread === 1 ? "thing needs" : "things need"} you` : g.act.length ? "All caught up" : "Nothing urgent";
    const span = { 1: "today", 7: "this week", 30: "this month" }[S.days];
    const row = (e) => {
      const m = e.reaction ? e.reaction.ret : e.px ? e.px.ret : null;
      return `<button class="item ${S.seen.has(e.id) ? "read" : ""}" data-open="${esc(e.id)}">
        <span class="l1">${e._b === "act" && !S.seen.has(e.id) ? '<span class="new" aria-label="new"></span>' : ""}<span class="sym">${esc(e.symbol)}</span><span>${esc(CAT[e.category] || "")}</span><span class="t">${ago(e.time)}</span></span>
        <span class="l2">${esc(tidy(e.so_what))}</span>
        <span class="l3"><span>${esc(whyLine(e))}</span>${m != null ? `<span class="n ${cls(m)}">${pct(m)}</span>` : ""}</span>
      </button>`;
    };
    const sec = (title, list, empty) => `<section class="sec"><h2><span>${title}</span><span>${list.length || ""}</span></h2>
      <div class="rows">${list.length ? list.map(row).join("") : `<div class="none">${empty}</div>`}</div></section>`;
    const holdings = S.feed.portfolio.map((h) => {
      const q = quoteOf(h.symbol) || {};
      return `<button class="hold" data-research="${h.symbol}"><span><b>${esc(h.symbol)}</b>${h.name.toUpperCase() !== h.symbol ? `<small>${esc(h.name)}</small>` : ""}</span>
        <span class="sp" data-q="${h.symbol}" data-f="spark">${daySpark(q.day, q.prev)}</span>
        <span class="p n" data-q="${h.symbol}" data-f="price">${priceHTML(q)}</span><span class="c n ${cls(q.chg)}" data-q="${h.symbol}" data-f="chg">${pct(q.chg, 2)}</span></button>`;
    }).join("");

    return `<header class="top"><div class="bar"><span class="brand">Delta</span>${statusLine()}</div>
        <p class="date">${new Date().toLocaleDateString("en-IN", { ...IST, weekday: "long", day: "numeric", month: "long" })}</p>
        <h1>${title}</h1>
        ${niftyLine()}
        <p class="lede">${g.look.length} more worth a look ${span}. ${g.rt.length} routine filings set aside.</p>${filingsStamp()}</header>
      <div class="tabsw" role="group" aria-label="Period">${[[1, "Today"], [7, "Week"], [30, "Month"]].map(([d, l]) => `<button data-days="${d}" aria-pressed="${S.days === d}">${l}</button>`).join("")}</div>
      ${sec("Needs you", g.act, "Nothing crossed your alert line.")}
      ${sec("Worth a look", g.look, "Nothing else stood out.")}
      ${g.rt.length ? `<section class="sec"><button class="more" data-routine aria-expanded="${S.routineOpen}"><span>${g.rt.length} routine filings</span><span>${S.routineOpen ? "Hide" : "Show"}</span></button>
        ${S.routineOpen ? g.rt.slice(0, 150).map((e) => `<button class="small" data-open="${esc(e.id)}"><span><b>${esc(e.symbol)}</b></span><span>${esc(e.so_what)}</span></button>`).join("") : ""}</section>` : ""}
      ${headlines()}
      <section class="sec"><h2><span>Your holdings</span><span>${S.live && S.quotesAt ? (S.marketOpen ? "Live, every 30 seconds" : "Last traded") : "Last close"}</span></h2><div class="rows">${holdings}</div></section>`;
  }

  // ---------------------------------------------------------------- headlines (context only, never scored)
  const sinceT = (iso) => {
    const h = (Date.now() - D(iso)) / 36e5;
    return h < 1 ? Math.max(1, Math.round(h * 60)) + "m" : h < 24 ? Math.round(h) + "h" : Math.round(h / 24) + "d";
  };
  const newsRow = (n, sym) => `<a class="news" href="${esc(n.link)}" target="_blank" rel="noopener">
      <span class="l1">${sym ? `<span class="sym">${esc(n.symbol)}</span>` : ""}<span>${esc(n.source)}</span>${n.time ? `<span class="t">${sinceT(n.time)}</span>` : ""}</span>
      <span class="l2">${esc(n.title)}</span></a>`;
  function headlines() {
    const all = (S.feed.news || []).filter((n) => !n.time || Date.now() - D(n.time) < Math.max(S.days, 3) * 864e5);
    if (!all.length) return "";
    const list = S.newsOpen ? all.slice(0, 40) : all.slice(0, 5);
    return `<section class="sec"><h2><span>In the news</span><span>${all.length}</span></h2><div class="rows">${list.map((n) => newsRow(n, true)).join("")}
      ${all.length > 5 ? `<button class="more" data-news aria-expanded="${S.newsOpen}"><span>${S.newsOpen ? "Fewer headlines" : `All ${Math.min(all.length, 40)} headlines`}</span><span></span></button>` : ""}</div>
      <p class="note">Headlines from Google News, for context. Delta's ranking and research use only company filings.</p></section>`;
  }

  // ---------------------------------------------------------------- detail
  function spark(sym, mark) {
    const p = S.feed.prices[sym]; if (!p) return "";
    const c = p.c.slice(), d = p.d.slice(), q = S.quotes[sym];
    if (q && q.time && q.time.slice(0, 10) > d[d.length - 1]) { c.push(q.price); d.push(q.time.slice(0, 10)); }
    const W = 320, H = 64, n = c.length, lo = Math.min(...c), hi = Math.max(...c);
    const x = (i) => 2 + (i / (n - 1)) * (W - 4), y = (v) => 4 + (1 - (v - lo) / (hi - lo || 1)) * (H - 8);
    const mi = mark ? d.findIndex((v) => v >= mark) : -1;
    return `<svg class="chart" viewBox="0 0 ${W} ${H + 14}" role="img" aria-label="${sym}, last ${n} sessions">
      <polyline points="${c.map((v, i) => x(i).toFixed(1) + "," + y(v).toFixed(1)).join(" ")}" fill="none" stroke="var(--ink)" stroke-width="1.4"/>
      ${mi >= 0 ? `<circle cx="${x(mi)}" cy="${y(c[mi])}" r="3.5" fill="var(--alert)"/>` : ""}
      <text x="2" y="${H + 12}">${dm(d[0])}</text><text x="${W - 2}" y="${H + 12}" text-anchor="end">${dm(d[n - 1])}</text></svg>
      <p class="note">Range ₹${inr(lo)} to ₹${inr(hi)} over ${n} sessions${mi >= 0 ? ". The dot is this event" : ""}.</p>`;
  }

  function why(e) {
    const f = e.factors || {}, r = [];
    if (e.category === "price") {
      r.push(["Size of the move and the volume", f.base], [`Your position (${holding(e.symbol).weight}% of portfolio)`, "× " + f.position]);
      if (f.unexplained) r.push(["No filing explains it", "+ " + f.unexplained]);
    } else {
      r.push([`${CAT[e.category] || e.category} starts at`, f.base]);
      if (f.magnitude !== 1) r.push(["Size compared with the company", "× " + f.magnitude]);
      if (f.surprise !== 1) r.push([f.surprise < 1 ? "In line with its trend" : "Breaks from its trend", "× " + f.surprise]);
      r.push([`Your position (${holding(e.symbol).weight}% of portfolio)`, "× " + f.position]);
      if (f.reaction) r.push(["The market reacted", "+ " + f.reaction]);
      if (f.cluster) r.push(["Other news this week", "+ " + f.cluster]);
    }
    return `<div class="block"><h3>Why it's ranked here</h3><div class="calc">${r.map(([k, v]) => `<span>${esc(k)}</span><span>${esc(v)}</span>`).join("")}
      <span class="tot">Score</span><span class="tot">${e.score} of 100</span></div>
      <p class="note" style="margin-top:8px">Needs you from ${S.rules.alert_score}. Change it in Alerts.</p></div>`;
  }

  function renderDetail(e) {
    const b = [];
    if (e.results) {
      const r = e.results;
      b.push(`<div class="block"><h3><span>${esc(r.quarter)}, ₹ crore</span><a href="${esc(r.source)}" target="_blank" rel="noopener">Source</a></h3>
        <div class="wide"><table class="tbl"><thead><tr><th></th><th>This quarter</th><th>vs last qtr</th><th>vs last year</th></tr></thead><tbody>
        ${r.rows.map((x) => `<tr><td>${esc(x.label)}</td><td>${inr(x.now)}</td><td class="${cls(x.qoq)}">${pct(x.qoq)}</td><td class="${cls(x.yoy)}">${pct(x.yoy)}</td></tr>`).join("")}
        </tbody></table></div>
        ${r.margin ? `<p class="note" style="margin-top:10px">Margin ${r.margin.now}%, ${r.margin.bps_yoy >= 0 ? "up" : "down"} ${Math.abs(r.margin.bps_yoy)} basis points on a year ago.${r.pat_trend_yoy != null ? ` Profit had been growing ${pct(r.pat_trend_yoy, 0)} a year over the previous four quarters.` : ""}</p>` : ""}</div>`);
    }
    if (e.management) {
      const m = e.management;
      b.push(`<div class="block"><h3>Who</h3><dl class="kv"><dt>Name</dt><dd>${esc(m.person || "In the filing")}</dd>
        <dt>Role</dt><dd>${esc(m.title || m.role)}${m.subsidiary ? ", at a subsidiary" : ""}</dd><dt>Change</dt><dd>${esc(m.action)}${m.abrupt ? ", <span class=\"down\">abruptly</span>" : ""}</dd></dl>
        ${m.bio ? `<p style="margin-top:10px">${esc(m.bio)}</p><p class="note">From the profile the company filed.</p>` : ""}</div>`);
    }
    if (e.legal && (e.legal.authority || e.legal.action)) {
      const L = e.legal;
      b.push(`<div class="block"><h3>The order</h3><dl class="kv">${L.authority ? `<dt>From</dt><dd>${esc(L.authority)}</dd>` : ""}${L.action ? `<dt>What</dt><dd>${esc(L.action)}</dd>` : ""}
        ${L.direction ? `<dt>For or against</dt><dd class="${L.direction === "favourable" ? "up" : "down"}">${L.direction === "favourable" ? "For the company" : "Against the company"}</dd>` : ""}${L.impact ? `<dt>Impact</dt><dd>${esc(L.impact)}</dd>` : ""}</dl></div>`);
    }
    if (e.magnitude && e.magnitude.amount_cr) {
      const m = e.magnitude;
      b.push(`<div class="block"><h3>How big</h3><div class="trio"><div><b>₹${inr(m.amount_cr)} cr</b><span>${m.band ? esc(m.band) + " band" : "amount"}</span></div>
        ${m.pct_revenue != null ? `<div><b>${m.pct_revenue}%</b><span>of a year's revenue</span></div>` : ""}${m.pct_mcap != null ? `<div><b>${m.pct_mcap}%</b><span>of market value</span></div>` : ""}</div>
        ${m.band ? `<p class="note" style="margin-top:8px">L&T discloses order size as a band. Delta uses its midpoint.</p>` : ""}</div>`);
    }
    const body = !e.results && (e.gist || e.summary || (e.text !== e.subject ? e.text : ""));
    if (body && !(e.management && e.management.bio)) b.push(`<div class="block"><h3>From the filing</h3><p>${esc(body)}</p></div>`);
    const rx = e.reaction || (e.px ? { ret: e.px.ret, vol_x: e.px.vx, rel: e.px.rel, date: e.px.date } : null);
    b.push(`<div class="block"><h3>How the stock reacted</h3>${rx ? `<div class="trio"><div><b class="${cls(rx.ret)}">${pct(rx.ret)}</b><span>that day</span></div>
      <div><b>${(rx.vol_x || 0).toFixed(1)}×</b><span>usual volume</span></div><div><b class="${cls(rx.rel)}">${pct(rx.rel)}</b><span>vs Nifty 50</span></div></div>` : `<p class="note">No trading day after this yet.</p>`}
      ${spark(e.symbol, rx && rx.date)}</div>`);
    b.push(why(e));
    if (e.related && e.related.length) b.push(`<div class="block"><h3>Also filed with it</h3><div class="links">${e.related.map((r) => r.pdf ? `<a href="${esc(r.pdf)}" target="_blank" rel="noopener">${esc(r.text || r.subject)}</a>` : `<span>${esc(r.text || r.subject)}</span>`).join("")}</div></div>`);
    const key = e.symbol + ":" + e.category;
    return `<div class="sheet" role="dialog" aria-modal="true" aria-label="${esc(e.symbol)}"><div class="sheet-in">
      <button class="back" data-close>‹ Back</button>
      <p class="meta">${esc(holding(e.symbol).name)} · ${esc(CAT[e.category] || "")} · ${dm(e.time)}${e.category === "price" ? "" : ", " + hm(e.time)}</p>
      <h1>${esc(tidy(e.so_what))}</h1>
      ${b.join("")}
      <div class="btns">
        ${e.results ? `<button class="btn go" data-research="${e.symbol}">Did they deliver what they promised?</button>` : `<button class="btn go" data-research="${e.symbol}">Research ${esc(e.symbol)}</button>`}
        ${e.pdf ? `<a class="btn" href="${esc(e.pdf)}" target="_blank" rel="noopener">Read the filing</a>` : ""}
        <button class="btn" data-seen="${esc(e.id)}">${S.seen.has(e.id) ? "Mark as unread" : "Done, mark as read"}</button>
        <button class="btn" data-less="${esc(key)}">${S.less.has(key) ? "Show these again" : `Show fewer like this from ${esc(e.symbol)}`}</button>
      </div></div></div>`;
  }

  // ---------------------------------------------------------------- research
  // Re-draw the page without losing the reader's place: same scroll position, same quotes left open.
  function keepPlace(draw) {
    const open = [...document.querySelectorAll("#main details")].map((d) => d.open), y = window.scrollY;
    draw();
    document.querySelectorAll("#main details").forEach((d, i) => { if (open[i]) d.open = true; });
    window.scrollTo(0, y);
  }

  // Research opens at once from the copy saved with the app, when this company has one, and the live
  // build from the server then replaces it in place. A company with no saved copy waits for the live build.
  async function loadResearch(sym, fresh) {
    sym = String(sym || "").toUpperCase().replace(/[^A-Z0-9&-]/g, "");
    if (!sym) return;
    S.rsym = sym; store.set("rsym", sym); S.rerr = null; S.loading = false;
    const here = () => S.rsym === sym;                       // the reader may have moved on to another company
    const show = () => { if (here() && S.tab === "research") render(); };
    if (!S.research[sym]) {
      S.loading = true; show();
      const listed = S.index.some((x) => x.symbol === sym);   // only ask for a saved copy that exists
      const saved = listed ? await getJSON(`data/research/${sym}.json`).catch(() => null) : null;
      if (saved && !saved.error && !S.research[sym]) { saved._saved = true; S.research[sym] = saved; }
      if (S.research[sym] && here()) { S.loading = false; show(); }
    }
    const have = S.research[sym];
    if (S.live && (!have || have._saved || fresh) && !S.rbusy.has(sym)) {
      S.rbusy.add(sym);
      let r = null;
      try { r = await getJSON(`api/research?symbol=${encodeURIComponent(sym)}${fresh ? "&fresh=1" : ""}`); } catch (e) { /* server asleep or busy */ }
      S.rbusy.delete(sym);
      if (r && !r.error) S.research[sym] = r;
      else if (S.research[sym] && S.research[sym]._saved) S.research[sym]._failed = true;  // keep the saved copy, and say so
      if (here()) {
        S.loading = false;
        if (!S.research[sym]) S.rerr = r && r.error ? r.error : `Couldn't reach the server for ${sym}. Try again in a moment.`;
        if (S.tab === "research") { keepPlace(renderMain); poll(false); }
      }
      return;
    }
    if (here()) {
      S.loading = !S.research[sym] && S.rbusy.has(sym);     // a live build for this company is already on its way
      if (!S.research[sym] && !S.loading) S.rerr = `${sym} needs the live server. This copy has ${S.index.length} companies saved; try one below.`;
      show();
      if (S.tab === "research") poll(false);
    }
  }

  function bars(labels, vals, line) {
    const W = 320, H = 120, n = vals.length, vmax = Math.max(...vals.map((v) => v || 0)) || 1;
    const x = (i) => 10 + (i + 0.5) * ((W - 20) / n), bw = ((W - 20) / n) * 0.55;
    const lv = (line || []).filter((v) => v != null), lmin = Math.min(...lv), lmax = Math.max(...lv);
    const ly = (v) => 18 + (1 - (v - lmin) / (lmax - lmin || 1)) * 34;
    return `<svg class="chart" viewBox="0 0 ${W} ${H + 4}" role="img" aria-label="Revenue bars and margin line by year">
      ${vals.map((v, i) => { const h = ((v || 0) / vmax) * (H - 70); return `<rect class="bar" x="${(x(i) - bw / 2).toFixed(1)}" y="${(H - 14 - h).toFixed(1)}" width="${bw.toFixed(1)}" height="${h.toFixed(1)}" rx="2"/>`; }).join("")}
      <line class="axis" x1="0" x2="${W}" y1="${H - 14}" y2="${H - 14}"/>
      ${line ? `<polyline class="ln" points="${line.map((v, i) => v == null ? "" : x(i).toFixed(1) + "," + ly(v).toFixed(1)).filter(Boolean).join(" ")}"/>
        ${line.map((v, i) => v == null ? "" : `<circle class="pt" cx="${x(i).toFixed(1)}" cy="${ly(v).toFixed(1)}" r="2.5"/><text x="${x(i).toFixed(1)}" y="${(ly(v) - 7).toFixed(1)}" text-anchor="middle">${v}%</text>`).join("")}` : ""}
      ${labels.map((l, i) => `<text x="${x(i).toFixed(1)}" y="${H + 2}" text-anchor="middle">${esc(l)}</text>`).join("")}</svg>`;
  }
  function conv(labels, vals) {
    const W = 320, H = 100, n = vals.length, top = Math.max(150, ...vals.map((v) => v || 0));
    const x = (i) => 10 + (i + 0.5) * ((W - 20) / n), bw = ((W - 20) / n) * 0.5, y = (v) => 14 + (1 - Math.max(0, v) / top) * (H - 30);
    return `<svg class="chart" viewBox="0 0 ${W} ${H + 4}" role="img" aria-label="Cash from operations as a share of profit">
      <line class="ref" x1="0" x2="${W}" y1="${y(100)}" y2="${y(100)}"/>
      ${vals.map((v, i) => v == null ? "" : `<rect class="bar ${v < 70 ? "low" : ""}" x="${(x(i) - bw / 2).toFixed(1)}" y="${y(v).toFixed(1)}" width="${bw.toFixed(1)}" height="${(H - 16 - y(v)).toFixed(1)}" rx="2"/><text x="${x(i).toFixed(1)}" y="${(y(v) - 4).toFixed(1)}" text-anchor="middle">${v}%</text>`).join("")}
      <line class="axis" x1="0" x2="${W}" y1="${H - 16}" y2="${H - 16}"/>${labels.map((l, i) => `<text x="${x(i).toFixed(1)}" y="${H}" text-anchor="middle">${esc(l)}</text>`).join("")}</svg>`;
  }
  const CS = { matches: ["Matches", "good"], close: ["Close", "mid"], differs: ["Doesn't match", "bad"] };
  const QS = { ok: ["Clean", "good"], watch: ["Watch", "mid"], flag: ["Flag", "bad"] };
  const callName = (p) => p ? p + " call" : "call";

  function claimsBlock(c) {
    if (!c) return "";
    const items = c.items || [];
    const head = `<h3><span>What they said vs what they reported</span></h3>`;
    if (!items.length) return `<div class="block">${head}<p class="note">${esc(c.summary || "No call transcript to check.")}</p></div>`;
    const rows = items.slice(0, 8).map((x) => {
      const [vl, vc] = CS[x.status] || ["", "wait"];
      return `<div class="claim"><div class="h"><span>${esc(x.said)}</span><span class="v ${vc}">${vl}</span></div>
        <p class="why">Reported: ${esc(x.found)} <span class="dim">· ${esc(x.quarter)} quarter, ${esc(x.statements)}</span></p>
        <details><summary>Their words</summary><blockquote>“${esc(x.quote)}” <a href="${esc(x.source)}" target="_blank" rel="noopener">${esc(callName(x.call))}, page ${x.page}</a></blockquote>
        <p class="note" style="margin-top:6px"><a href="${esc(x.actual_source)}" target="_blank" rel="noopener">Reported quarterly results</a></p></details></div>`;
    }).join("");
    return `<div class="block">${head}<p class="score-line">${esc(c.summary)}</p>${rows}
      <p class="note" style="margin-top:8px">${esc(c.note || "")}</p></div>`;
  }

  function qualityBlock(q) {
    if (!q || !(q.checks || []).length) return "";
    return `<div class="block"><h3><span>Is the profit real?</span></h3><p class="score-line">${esc(q.summary)}</p>
      ${q.checks.map((k) => { const [vl, vc] = QS[k.status] || ["", "wait"]; return `<div class="claim"><div class="h"><span>${esc(k.title)}</span><span class="v ${vc}">${vl}</span></div>
        <p class="why">${esc(k.text)} <a href="${esc(k.src)}" target="_blank" rel="noopener">Source</a></p></div>`; }).join("")}
      <p class="note" style="margin-top:8px">Eight rules over the cash flow statement, balance sheet, results and shareholding.</p></div>`;
  }

  function priceLine(r) {
    const q = S.quotes[r.symbol] || r.quote;
    if (!q || q.price == null) return "";
    if (!S.quotes[r.symbol]) S.quotes[r.symbol] = q;
    const when = S.live && S.marketOpen ? "live, every 30 seconds" : q.time ? "last traded " + dm(q.time) + ", " + hm(q.time) : "";
    return `<p class="px"><b class="n" data-q="${r.symbol}" data-f="price">₹${inr(q.price, 2)}</b> <span class="n ${cls(q.chg)}" data-q="${r.symbol}" data-f="chg">${pct(q.chg, 2)}</span>
      <span class="sp" data-q="${r.symbol}" data-f="spark">${daySpark(q.day, q.prev)}</span> <span class="dim">${when}</span></p>`;
  }

  const V = { kept: ["Kept", "good"], "on track": ["On track", "good"], partly: ["Partly", "mid"], missed: ["Missed", "bad"], moved: ["Moved", "bad"], pending: ["Not due", "wait"] };

  function credLine(cred) {
    const c = cred.counts || {}, good = (c.kept || 0) + (c["on track"] || 0);
    const rest = [["partly", "partly"], ["missed", "missed"], ["moved", "moved"]].filter(([k]) => c[k]).map(([k, l]) => `${c[k]} ${l}`);
    return `${good} of ${cred.of} ${cred.of === 1 ? "promise" : "promises"} kept or on track${rest.length ? "; " + rest.join(", ") : ""}.`;
  }

  function researchBody(r) {
    const s = r.snapshot, t = r.trajectory, c = r.cash, b = r.balance, g = r.guidance || {}, items = g.items || [];
    const cred = r.verdict && r.verdict.credibility, out = [];
    const savedNote = r._saved && S.live ? `<p class="stamp">Saved copy${r.generated ? " from " + dm(r.generated) + ", " + hm(r.generated) : ""}. ${r._failed ? "Couldn't update it just now." : "Updating…"}</p>` : "";
    out.push(`<div class="rname"><h1>${esc(r.name)}</h1>${priceLine(r)}${savedNote}<p class="lede">${esc(r.about)}</p></div>`);
    out.push(`<div class="block"><div class="trio"><div><b>₹${inr(s.revenue_ttm)}</b><span>crore revenue, last 12 months</span></div>
      <div><b>${s.op_margin_ttm == null ? "–" : s.op_margin_ttm + "%"}</b><span>${esc(t.op_label)}</span></div>
      <div><b>₹${inr(s.pat_ttm)}</b><span>crore profit</span></div></div></div>`);
    out.push(claimsBlock(r.claims));
    if (g.mode === "extracted" && items.length) {
      out.push(`<div class="block"><h3>What they promised, and what happened</h3>
        ${cred ? `<p class="score-line">${credLine(cred)}</p>` : ""}
        ${items.map((x) => { const [vl, vc] = V[x.verdict] || ["", "wait"]; return `<div class="promise">
          <div class="h"><span>${esc(x.metric)}</span><span class="v ${vc}">${vl}</span></div>
          <dl class="kv"><dt>Promised</dt><dd>${esc(x.promise)} <span class="dim">(${esc(x.said_in)})</span></dd><dt>Result</dt><dd>${esc(x.actual || "Not due yet")}</dd></dl>
          ${x.why ? `<p class="why">${esc(x.why)}</p>` : ""}
          <details><summary>Their words</summary><blockquote>“${esc(x.quote)}” <a href="${esc(x.source)}" target="_blank" rel="noopener">${esc(x.said_in)}, page ${x.page ?? "?"}</a></blockquote>
          ${x.actual_quote ? `<blockquote>“${esc(x.actual_quote)}” <a href="${esc(x.actual_source)}" target="_blank" rel="noopener">${esc(x.actual_said_in || "Later call")}, page ${x.actual_page ?? "?"}</a></blockquote>`
            : x.actual_source ? `<p class="note" style="margin-top:6px"><a href="${esc(x.actual_source)}" target="_blank" rel="noopener">Reported quarterly results</a></p>` : ""}</details></div>`; }).join("")}
        <p class="note" style="margin-top:8px">${g.note ? esc(g.note) + " " : ""}Promises are read from the company's own calls and graded against what it reported later. Ones not due yet are not counted.</p></div>`);
    } else if (items.length) {
      out.push(`<div class="block"><h3>What management said on recent calls</h3>
        ${items.slice(0, 4).map((x) => `<div class="promise"><p class="why">“${esc(x.quote)}”</p><a class="note" href="${esc(x.source)}" target="_blank" rel="noopener">${esc(x.said_in)} call, page ${x.page}</a></div>`).join("")}
        <p class="note" style="margin-top:8px">Not graded yet. Grading runs when the server has a model key.</p></div>`);
    }
    out.push(qualityBlock(r.quality));
    out.push(`<div class="block"><h3><span>Growth</span><a href="${esc(t.source)}" target="_blank" rel="noopener">Source</a></h3>
      ${bars(t.years, t.revenue, t.op_margin)}
      <p class="note">Bars are revenue, the line is ${esc(t.op_label.toLowerCase())}. Revenue grew ${t.rev_cagr_5y != null ? pct(t.rev_cagr_5y) + " a year over five years and " : ""}${pct(t.rev_cagr_3y)} over three.</p></div>`);
    out.push(c.applicable ? `<div class="block"><h3><span>Does profit turn into cash?</span><a href="${esc(c.source)}" target="_blank" rel="noopener">Source</a></h3>
      ${conv(c.years, c.cfo_to_pat)}<p class="note">Cash from operations as a share of profit. 100% means every rupee of profit arrived as cash; red bars are under 70%. Three-year average ${c.avg3}%.</p></div>`
      : `<div class="block"><h3>Does profit turn into cash?</h3><p class="note">Not a useful test for a lender: its operating cash flow is mostly new loans going out.</p></div>`);
    const has = (a) => (a || []).some((v) => v != null);
    out.push(`<div class="block"><h3><span>Debt and working capital</span><a href="${esc(b.source)}" target="_blank" rel="noopener">Source</a></h3>
      <div class="wide"><table class="tbl"><thead><tr><th></th>${b.cols.map((x) => `<th>${esc(x)}</th>`).join("")}</tr></thead><tbody>
      <tr><td>Debt, ₹ cr</td>${b.debt.map((v) => `<td>${inr(v)}</td>`).join("")}</tr>
      <tr><td>Debt / equity</td>${b.de.map((v) => `<td>${v ?? "–"}</td>`).join("")}</tr>
      ${has(b.wc_days) ? `<tr><td>Working capital days</td>${b.wc_days.map((v) => `<td>${v ?? "–"}</td>`).join("")}</tr>` : ""}
      ${has(b.debtor_days) ? `<tr><td>Days to get paid</td>${b.debtor_days.map((v) => `<td>${v ?? "–"}</td>`).join("")}</tr>` : ""}</tbody></table></div></div>`);
    out.push(`<div class="block"><h3>For and against</h3><div class="pair">
      <div><h4 class="up">For</h4>${r.bull.length ? `<ul>${r.bull.map((x) => `<li>${esc(x.text)}. <a href="${esc(x.src)}" target="_blank" rel="noopener">Source</a></li>`).join("")}</ul>` : `<p class="note">Nothing stands out.</p>`}</div>
      <div><h4 class="down">Against</h4>${r.bear.length ? `<ul>${r.bear.map((x) => `<li>${esc(x.text)}. <a href="${esc(x.src)}" target="_blank" rel="noopener">Source</a></li>`).join("")}</ul>` : `<p class="note">No warning signs in growth, margins, cash, debt or promoter holding.</p>`}</div></div></div>`);
    if ((r.news || []).length) out.push(`<div class="block"><h3><span>In the news this week</span></h3><div class="rows nb">${r.news.map((n) => newsRow(n, false)).join("")}</div>
      <p class="note" style="margin-top:8px">Headlines for context only. Nothing above uses them.</p></div>`);
    const so = r.sources;
    out.push(`<div class="block"><h3>Sources</h3><div class="links">
      <a href="${esc(so.screener)}" target="_blank" rel="noopener">Financial statements, from annual reports and quarterly results</a>
      ${so.annual_report ? `<a href="${esc(so.annual_report.url)}" target="_blank" rel="noopener">${esc(so.annual_report.label)}</a>` : ""}
      ${(so.transcripts || []).map((x) => `<a href="${esc(x.transcript)}" target="_blank" rel="noopener">Earnings call, ${esc(x.period)}</a>`).join("")}</div>
      <p class="note" style="margin-top:8px">The analysis uses only the company's own documents. No news or analyst opinions.</p></div>`);
    return out.join("");
  }

  function renderResearch() {
    const r = S.research[S.rsym];
    const port = new Set(S.feed.portfolio.map((p) => p.symbol));
    const extra = S.index.filter((x) => !port.has(x.symbol)).map((x) => x.symbol);
    const graded = S.index.filter((x) => x.credibility).map((x) => x.symbol);
    return `<header class="top"><div class="bar"><span class="brand">Delta</span>${statusLine()}</div>
        <h1 style="margin-top:18px">Research</h1><p class="lede">Any NSE company, from its own filings.</p></header>
      <form class="search" data-search><input id="q" name="q" placeholder="Ticker, e.g. TRENT" autocomplete="off" autocapitalize="characters" enterkeyhint="search"><button type="submit">Go</button></form>
      <p class="try">Try ${[...new Set(graded.concat(extra))].map((s) => `<button data-rsym="${s}">${s}</button>`).join("")}</p>
      ${S.loading ? `<p class="none">Reading ${esc(S.rsym)}'s filings…</p>` : S.rerr ? `<p class="none">${esc(S.rerr)}</p>` : r ? researchBody(r) : ""}`;
  }

  // ---------------------------------------------------------------- alerts
  function renderRules() {
    const R = S.rules, muted = new Set(R.muted_categories);
    return `<header class="top"><div class="bar"><span class="brand">Delta</span>${statusLine()}</div>
        <h1 style="margin-top:18px">Alerts</h1><p class="lede">You decide what counts as unusual.</p></header>
      <div class="field"><label for="r-px">A price moves more than <span>${R.price_move_pct}%</span></label>
        <input id="r-px" type="range" min="2" max="10" step="0.5" value="${R.price_move_pct}" data-rule="price_move_pct"><small>In one day, either way.</small></div>
      <div class="field"><label for="r-vol">Volume is more than <span>${R.volume_multiple}× usual</span></label>
        <input id="r-vol" type="range" min="1.5" max="5" step="0.1" value="${R.volume_multiple}" data-rule="volume_multiple"><small>Usual means the 20-day average.</small></div>
      <div class="field"><label for="r-alert">Tell me when the score is at least <span>${R.alert_score}</span></label>
        <input id="r-alert" type="range" min="40" max="90" step="5" value="${R.alert_score}" data-rule="alert_score"><small>Lower shows you more. Every item shows how its score was worked out.</small></div>
      <section class="sec"><h2><span>Filings to watch</span></h2><div class="rows">
        ${CARE.map((c) => `<button class="toggle" data-cat="${c}" aria-pressed="${!muted.has(c)}"><span>${esc(CAT[c])}</span><span class="sw"></span></button>`).join("")}</div>
        <p class="note">Switched-off types still appear under routine filings.</p>
        ${S.less.size ? `<button class="more" data-clearless><span>Undo ${S.less.size} “show fewer” choice${S.less.size > 1 ? "s" : ""}</span><span></span></button>` : ""}</section>
      <section class="sec"><h2><span>Appearance</span></h2><div class="tabsw" role="group" aria-label="Theme">
        ${["system", "light", "dark"].map((t) => `<button data-theme="${t}" aria-pressed="${store.get("theme", "system") === t}">${t[0].toUpperCase() + t.slice(1)}</button>`).join("")}</div></section>`;
  }

  // ---------------------------------------------------------------- shell
  const ICON = {
    today: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><path d="M4 7h16M4 12h16M4 17h10"/></svg>',
    research: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><circle cx="10.5" cy="10.5" r="6"/><path d="m15 15 5 5"/></svg>',
    rules: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><path d="M6 9a6 6 0 1 1 12 0c0 5 2 6.5 2 6.5H4S6 14 6 9Z"/><path d="M10 19a2 2 0 0 0 4 0"/></svg>',
  };
  function renderMain() {
    $("#main").innerHTML = S.tab === "today" ? renderToday() : S.tab === "research" ? renderResearch() : renderRules();
  }
  function render() {
    if (!S.feed) return;
    renderMain();
    const unread = lists().act.some((e) => !S.seen.has(e.id));
    $("#tabs").innerHTML = `<nav aria-label="Main">${[["today", "Today"], ["research", "Research"], ["rules", "Alerts"]].map(([k, l]) =>
      `<button data-tab="${k}" ${S.tab === k ? 'aria-current="page"' : ""}>${ICON[k]}<span>${l}</span>${k === "today" && unread ? '<span class="dot"></span>' : ""}</button>`).join("")}</nav>`;
    const e = S.open && allItems().find((x) => x.id === S.open);
    $("#sheet").innerHTML = e ? renderDetail(e) : "";
    document.body.style.overflow = e ? "hidden" : "";
  }
  function go(tab) {
    S.tab = tab; S.open = null; try { history.replaceState(null, "", "#" + tab); } catch (e) { /* sandboxed */ }
    render(); window.scrollTo(0, 0);
    if (tab === "research" && !S.research[S.rsym]) loadResearch(S.rsym);
  }

  document.addEventListener("click", (ev) => {
    const t = ev.target.closest("button, a"); if (!t) return;
    const d = t.dataset;
    if (d.tab) return go(d.tab);
    if (d.open) { S.open = d.open; render(); $("#sheet .sheet")?.scrollTo(0, 0); return; }
    if (d.close !== undefined) { S.open = null; return render(); }
    if (d.days) { S.days = +d.days; store.set("days", S.days); return render(); }
    if (d.routine !== undefined) { S.routineOpen = !S.routineOpen; return render(); }
    if (d.news !== undefined) { S.newsOpen = !S.newsOpen; return render(); }
    if (d.research) { S.tab = "research"; S.open = null; try { history.replaceState(null, "", "#research"); } catch (e) {} window.scrollTo(0, 0); return loadResearch(d.research); }
    if (d.rsym) return loadResearch(d.rsym);
    if (d.refresh !== undefined) { refreshAll(true); return; }
    if (d.seen) {
      S.seen.has(d.seen) ? S.seen.delete(d.seen) : S.seen.add(d.seen); store.set("seen", [...S.seen]);
      const done = S.seen.has(d.seen); S.open = null; render(); return toast(done ? "Marked as read" : "Marked as unread");
    }
    if (d.less) {
      const had = S.less.has(d.less); had ? S.less.delete(d.less) : S.less.add(d.less); store.set("less", [...S.less]);
      S.open = null; render(); return toast(had ? "These will show again" : "You'll see fewer of these");
    }
    if (d.cat) {
      const m = new Set(S.rules.muted_categories); m.has(d.cat) ? m.delete(d.cat) : m.add(d.cat);
      S.rules.muted_categories = [...m]; store.set("rules", S.rules); return render();
    }
    if (d.clearless !== undefined) { S.less.clear(); store.set("less", []); return render(); }
    if (d.theme) { store.set("theme", d.theme); applyTheme(); return render(); }
  });
  document.addEventListener("input", (ev) => {
    const k = ev.target.dataset.rule; if (!k) return;
    S.rules[k] = +ev.target.value; store.set("rules", S.rules);
    const lab = ev.target.previousElementSibling?.querySelector("span");
    if (lab) lab.textContent = k === "price_move_pct" ? `${S.rules[k]}%` : k === "volume_multiple" ? `${S.rules[k]}× usual` : S.rules[k];
  });
  document.addEventListener("submit", (ev) => {
    if (!ev.target.matches("[data-search]")) return;
    ev.preventDefault(); ev.target.querySelector("input").blur(); loadResearch(new FormData(ev.target).get("q"));
  });
  document.addEventListener("keydown", (ev) => { if (ev.key === "Escape" && S.open) { S.open = null; render(); } });

  function applyTheme() {
    const t = store.get("theme", "system");
    if (t === "system") document.documentElement.removeAttribute("data-theme"); else document.documentElement.setAttribute("data-theme", t);
  }

  // ---------------------------------------------------------------- data: live server first, saved copy second
  async function getJSON(url) {
    const r = await fetch(url, { cache: "no-store" });
    if (!r.ok || !(r.headers.get("content-type") || "").includes("json")) throw new Error(url);
    return r.json();
  }
  async function loadFeed() {
    try {
      const f = await getJSON("api/feed");
      if (f && f.events) { S.feed = f; S.live = true; S.feedLive = (f.ready ?? f.live) !== false; S.marketOpen = !!f.market_open; return; }
    } catch (e) { /* no server here */ }
    if (!S.feed) { S.feed = await getJSON("data/feed.json"); S.live = false; }
  }
  // one small request every 30 seconds: prices for everything on screen, and whether the feed changed
  let busy = false;
  async function poll(force) {
    if (!S.live || busy) return;
    busy = true;
    try {
      const watch = S.tab === "research" ? `?watch=${encodeURIComponent(S.rsym)}` : "";
      const L = await getJSON("api/live" + watch);
      Object.assign(S.quotes, L.quotes || {});
      S.quotesAt = L.quotes_at; S.checkedAt = L.checked_at; S.marketOpen = !!L.market_open; S.every = L.every;
      const woke = !S.feedLive && (L.ready ?? L.live) !== false;  // the server has finished its first refresh
      if (force || woke || (L.feed_generated && L.feed_generated !== S.feed.generated)) {
        await loadFeed();
        if (S.tab === "today") keepPlace(renderMain);
        if (force && S.tab === "research") loadResearch(S.rsym, true);
      }
      patchPrices();
    } catch (e) { /* server busy; try again on the next beat */ }
    busy = false;
    S.nextAt = Date.now() + POLL;
  }
  async function refreshAll(force) { await poll(force); if (force) toast("Up to date"); }

  async function boot() {
    applyTheme();
    try {
      await loadFeed();
      S.index = await getJSON("data/research/index.json").catch(() => []);
      S.rules = Object.assign({}, S.feed.rules, store.get("rules", {}));
    } catch (e) {
      $("#main").innerHTML = `<p class="none" style="margin-top:32px">Delta couldn't load any data. Start the server with <b>python serve.py</b> and open the address it prints.</p>`;
      return;
    }
    const h = (location.hash || "").slice(1);
    if (["today", "research", "rules"].includes(h)) S.tab = h;
    render();
    if (S.tab === "research") loadResearch(S.rsym);
    if (S.live) {
      await poll(false); render();
      setInterval(() => { if (document.visibilityState === "visible") poll(false); }, POLL);
      setInterval(() => { const t = $("#tick"); if (t) t.textContent = tickText(); }, 1000);
      document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") poll(false); });
    }
  }
  window.addEventListener("hashchange", () => { const h = location.hash.slice(1); if (["today", "research", "rules"].includes(h) && h !== S.tab) go(h); });
  boot();
})();
