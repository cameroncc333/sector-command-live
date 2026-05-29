"""
telegram_bot.py — Sector Command notification + interactive command interface

SETUP (2 minutes, one time):
  1. Open Telegram → @BotFather → /newbot → name it "Sector Command"
  2. BotFather gives you a TOKEN (123456789:AAH...)   → TELEGRAM_TOKEN secret
  3. Search for your new bot, send it "hi"
  4. Visit https://api.telegram.org/bot<TOKEN>/getUpdates in a browser
  5. Find "chat":{"id": 987654321...} → that number is TELEGRAM_CHAT_ID secret
  6. Add both as GitHub Secrets (Settings → Secrets → Actions)

INTERACTIVE COMMANDS (reply to any message):
  BUY / SELL / SKIP [TICKER] [reason]    log decision + optional Alpaca paper order
  STATUS                                  last signal summary
  WHY                                     full reasoning trace
  PERF                                    paper P&L vs SPY
  REPORT                                  generate HTML report
  CRYPTO                                  live crypto mini-briefing
  GOLD                                    gold + macro signals
  PORTFOLIO                               your real holdings + P&L
  BALANCE 12500                           set your investable balance
  BOUGHT XLE 5 47.50                      log 5 shares of XLE at $47.50
  BOUGHT BTC-USD 500                      log $500 of BTC (shares auto-calculated)
  SOLD XLE                                remove XLE from holdings
  EXPLAIN XLE                             what is this sector/asset?
  HOW MUCH [TICKER]                       sizing guidance for a specific ticker
  Or just ask a question in plain English and the bot will answer it.
"""

import os
import json
import requests

TELEGRAM_TOKEN  = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
API_BASE = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

SECTOR_DESCRIPTIONS = {
    "XLK":  "Technology — Apple, Microsoft, Nvidia, etc. High growth, high beta. Sensitive to rates and AI trends.",
    "XLF":  "Financials — JPMorgan, BofA, Goldman. Benefits from rising rates, earnings-driven. Economically sensitive.",
    "XLV":  "Health Care — J&J, UnitedHealth, Pfizer. Defensive growth. FDA headlines matter.",
    "XLY":  "Consumer Discretionary — Amazon, Tesla, Home Depot. Consumer spending / economy. High VIX hurts it.",
    "XLP":  "Consumer Staples — P&G, Costco, Walmart. Defensive. Holds up well in downturns.",
    "XLE":  "Energy — Exxon, Chevron, SLB. Oil price and geopolitics drive it. High dividend yield.",
    "XLI":  "Industrials — Boeing, Caterpillar, UPS. Infrastructure + manufacturing. Cyclical.",
    "XLB":  "Materials — Freeport-McMoRan, Air Products. Mining, chemicals. Global growth sensitive.",
    "XLRE": "Real Estate — Prologis, AMT, Equinix. Rate-sensitive REITs. Hurt by high rates, love rate cuts.",
    "XLU":  "Utilities — NextEra, Duke, Sempra. Very defensive, high dividend. Bond-like behavior.",
    "XLC":  "Communication — Meta, Alphabet, Netflix. Ad revenue + streaming. Hybrid growth/defensive.",
    "SPY":  "S&P 500 ETF — the whole market. Used as 'neutral abstain' when sectors disagree.",
    "BIL":  "1-3 Month T-Bills ETF — essentially cash. Used as defensive 'crisis abstain'. VIX > 35 trigger.",
    "GLD":  "SPDR Gold ETF — inflation hedge, crisis protection. Runs when dollar weakens or fear spikes.",
    "TLT":  "20-Year Treasury ETF — long-duration bonds. Inverse relationship with rates. Macro hedge.",
    "QQQ":  "Invesco Nasdaq 100 ETF — tech-heavy broad market. High growth, higher volatility than SPY.",
    "BTC-USD": "Bitcoin — digital gold narrative. High-beta uncorrelated macro asset. 5% max position.",
    "ETH-USD": "Ethereum — smart-contract platform. Tracks BTC with higher vol. 5% max position.",
}


class TelegramBot:
    def __init__(self, token=None, chat_id=None):
        self.token   = token or TELEGRAM_TOKEN
        self.chat_id = chat_id or TELEGRAM_CHAT_ID
        self.api_base = f"https://api.telegram.org/bot{self.token}"
        if not self.token or not self.chat_id:
            print("[telegram_bot] No credentials — printing to console (dry-run mode).")

    def send_message(self, text, parse_mode="HTML", disable_preview=True):
        """Send a message, auto-chunking if > 4000 chars."""
        if not self.token or not self.chat_id:
            print("=== TELEGRAM (dry-run) ===")
            print(text)
            print("==========================")
            return {"ok": True, "dry_run": True}

        results = []
        for chunk in _chunk(text, 4000):
            payload = {"chat_id": self.chat_id, "text": chunk,
                       "disable_web_page_preview": disable_preview}
            if parse_mode:  # omit parse_mode entirely when None/empty — Telegram rejects null
                payload["parse_mode"] = parse_mode
            try:
                r = requests.post(f"{self.api_base}/sendMessage", json=payload, timeout=20)
                if not r.ok:
                    print(f"[telegram_bot] send failed {r.status_code}: {r.text[:120]}")
                results.append(r.json())
            except Exception as e:
                print(f"[telegram_bot] send failed: {e}")
                results.append({"ok": False, "error": str(e)})
        return results[-1] if results else {"ok": False}

    def send_briefing(self, briefing: dict):
        self.send_message(format_briefing(briefing))

    def get_updates(self, offset=None, timeout=0):
        params = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        r = requests.get(f"{self.api_base}/getUpdates", params=params, timeout=timeout + 10)
        return r.json()


# ── core briefing formatter ────────────────────────────────────────────────

def format_briefing(b: dict) -> str:
    """
    Full daily briefing. Shows:
      1. Market header (regime / VIX)
      2. Ranked opportunities (sectors + crypto + macro)
      3. Portfolio summary (if balance set)
      4. Data freshness
      5. Command menu
    """
    g = b.get
    lines = []
    lines.append("📡 <b>SECTOR COMMAND — Daily Briefing</b>")
    lines.append(f"📅 {g('date','—')}")

    regime = g('regime', 'NORMAL')
    vix    = g('vix', '—')
    regime_emoji = {"CALM": "🟢", "NORMAL": "🟡", "STRESSED": "🔴"}.get(str(regime).upper(), "⚪")
    lines.append(f"Market: {regime_emoji} <b>{regime}</b>  |  VIX: {vix}")

    # Abstain override notice
    if g('abstain_reason'):
        lines.append(f"⚠️ <b>Governance override:</b> {g('abstain_reason')}")

    lines.append("")

    # ── Ranked opportunities ───────────────────────────────────────────
    ranked = g("ranked_opportunities") or []
    if ranked:
        balance = None
        # Try to get balance for the header line
        try:
            from engine.position_tracker import PositionTracker
            balance = PositionTracker().get_balance()
        except Exception:
            pass

        lines.append("🎯 <b>TODAY'S CONVICTION TIERS</b>" +
                     (f"  (Portfolio: ${balance:,.0f})" if balance else ""))
        option_labels = ["A", "B", "C", "D", "E"]
        for i, opp in enumerate(ranked):
            lines.append(_format_opp(opp, option_labels[i]))
        lines.append("")
        lines.append("Reply <code>BUY A</code>, <code>BUY B</code>, etc. — or <code>BUY XLF</code> for any ticker.")
    else:
        # fallback: single RL pick
        action = g("action", "—")
        ticker = g("ticker", "—")
        conf   = g("confidence")
        lines.append("🧠 <b>RL Signal</b>")
        lines.append(f"  {action} <b>{ticker}</b>" + (f"  ({conf}% confidence)" if conf else ""))
        votes = g("rl_votes") or {}
        if votes:
            lines.append("  Agents: " + ", ".join(f"{k}:{v}" for k, v in votes.items()))
        lines.append("")

    # ── Equity alpha picks (top 3) ─────────────────────────────────────
    equity_picks = g("equity_alpha_picks") or []
    if equity_picks:
        lines.append("📈 <b>Stock Alpha Picks</b>  (factor model — individual stocks)")
        conv_emoji = {"HIGH": "🔥", "MEDIUM": "✅", "LOW": "🟡"}
        for i, p in enumerate(equity_picks[:3], 1):
            emoji   = conv_emoji.get(p.get("conviction"), "⚪")
            score   = p.get("composite_score", 0)
            sector  = p.get("sector_name", "")
            tagline = p.get("conviction_tagline", "")
            dollar  = f" → <b>${p['suggested_dollar']:.0f}</b>" if p.get("suggested_dollar") else ""
            lines.append(f"  {i}. {emoji} <b>{p['ticker']}</b> ({sector}) rank score {score:.0f}/100{dollar}")
            if tagline:
                lines.append(f"     💡 {tagline}")
        lines.append("  Reply <code>ALPHA</code> for full list with factor scores")
        lines.append("")

    # ── RL Agent Votes ────────────────────────────────────────────────
    rl_votes = g("rl_votes") or {}
    if rl_votes:
        vote_parts = []
        for agent, vote in rl_votes.items():
            vote_parts.append(f"{agent}:{vote}")
        lines.append("🤖 <b>Agent Votes:</b> " + "  |  ".join(vote_parts))
        lines.append("")

    # ── Supporting signals ─────────────────────────────────────────────
    lines.append("📊 <b>Supporting Signals</b>")
    ns = g("news_sentiment")
    if ns is not None:
        tone = "Bullish" if ns > 0.15 else "Bearish" if ns < -0.15 else "Neutral"
        lines.append(f"  News (FinBERT): {ns:+.2f}  [{tone}]")
    hl = g("news_headline")
    if hl:
        lines.append(f"  Top headline: \"{hl}\"")
    rsi = g("rsi")
    if rsi:
        lines.append(f"  RSI(14): {rsi}  |  Rel-strength: {g('rel_strength','—')}")
    if g("political_note"):
        lines.append(f"  🏛️ Political (research-only): {g('political_note')}")
    lines.append("")

    # ── Options PCR + VIX term structure ──────────────────────────────────
    opts = g("options_signals") or {}
    macro_for_vts = g("macro") or {}
    vts  = macro_for_vts.get("vix_term_structure", {})
    if vts and vts.get("ratio"):
        ts_regime = vts.get("ts_regime", "")
        ts_emoji  = "🔴" if ts_regime == "BACKWARDATION" else ("🟢" if ts_regime == "STEEP_CONTANGO" else "🟡")
        ev_str = "  ⚡event" if vts.get("event_risk") else ""
        lines.append(f"{ts_emoji} VIX: {vts.get('vix','?')} / VIX3M: {vts.get('vix3m','?')} "
                     f"(ratio {vts.get('ratio','?')}) [{ts_regime}]{ev_str}")
    if opts:
        spy_pcr = opts.get("SPY") or opts.get("QQQ")
        if spy_pcr:
            pcr_emoji = {"FEARFUL": "😰", "COMPLACENT": "😎", "NEUTRAL": "😐"}.get(
                spy_pcr.get("signal", "NEUTRAL"), "❓")
            lines.append(f"⚙️ PCR: {pcr_emoji} {spy_pcr.get('interpretation', '')}")
    if vts or opts:
        lines.append("")

    # ── StockTwits social sentiment ────────────────────────────────────────
    social = g("social_sentiment") or {}
    if social:
        soc_parts = []
        for tkr, s in list(social.items())[:3]:
            score = s.get("score", 0)
            sig   = s.get("signal", "NEUTRAL")
            emoji = "🟢" if sig == "BULLISH" else ("🔴" if sig == "BEARISH" else "⚪")
            soc_parts.append(f"{emoji}{tkr}({score:+.2f})")
        if soc_parts:
            lines.append(f"💬 StockTwits: {' · '.join(soc_parts)}")
            lines.append("")

    # ── FOMC window ────────────────────────────────────────────────────
    fomc = g("fomc_live") or {}
    if fomc.get("active"):
        label   = fomc.get("label", "PENDING")
        f_emoji = {"DOVISH": "🕊️", "HAWKISH": "🦅", "NEUTRAL": "⚖️"}.get(label, "⏳")
        lines.append(f"🏦 <b>FOMC ({fomc.get('meeting_date','')})</b>: {f_emoji} <b>{label}</b>  {fomc.get('sentiment','')}")
        if fomc.get("note"):
            lines.append(f"  {fomc['note']}")
        lines.append("")

    # ── Sell signals (urgent first) ───────────────────────────────────
    sell_text = g("sell_alerts")
    if sell_text:
        lines.append(sell_text)
        lines.append("")

    # ── Equity alpha conviction drops (individual stock exits) ────────
    alpha_exits = g("equity_alpha_exit_alerts") or []
    if alpha_exits:
        lines.append("📉 <b>Equity Alpha — Exit Signals</b>")
        for sig in alpha_exits:
            emoji = "🔴" if sig.get("urgency") == "URGENT" else "🟡"
            lines.append(f"  {emoji} <b>{sig['ticker']}</b> [{sig['signal_type']}]")
            lines.append(f"     {sig['detail']}")
        lines.append("  Reply <code>SOLD TICKER</code> after exiting.")
        lines.append("")

    # ── Earnings warnings ─────────────────────────────────────────────
    earn_text = g("earnings_warning")
    if earn_text:
        lines.append(earn_text)
        lines.append("")

    # ── Macro indicators ──────────────────────────────────────────────
    macro = g("macro") or {}
    yc  = macro.get("yield_curve", {})
    dxy = macro.get("dollar", {})
    if yc or dxy:
        lines.append("🌍 <b>Macro</b>")
        if yc:
            inv = " ⚠️ INVERTED" if yc.get("inverted") else ""
            short_label = yc.get("rate_label", "13w T-bill")
            lines.append(f"  Yield curve: {yc.get('spread','?')}%{inv}  (10yr {yc.get('ten_yr','?')}% − {short_label} {yc.get('two_yr','?')}%)")
        if dxy:
            chg = dxy.get("dxy_change", 0)
            lines.append(f"  Dollar (DXY): {dxy.get('dxy','?')}  ({chg:+.2f}%)")
        lines.append("")

    # ── Options hedge ──────────────────────────────────────────────────
    hedge = g("hedge_suggestion") or {}
    if hedge.get("triggered") and hedge.get("note"):
        lines.append(hedge["note"])
        lines.append("")

    # ── Alpaca paper account ───────────────────────────────────────────
    alp = g("alpaca_paper") or {}
    if alp.get("equity"):
        equity  = alp["equity"]
        cash    = alp.get("cash", 0)
        dpnl    = alp.get("daily_pnl", 0)
        dpnlPct = alp.get("daily_pnl_pct", 0)
        npos    = len(alp.get("positions", []))
        dpnl_emoji = "🟢" if dpnl >= 0 else "🔴"
        lines.append(f"🏦 <b>Alpaca Paper:</b> ${equity:,.0f}  |  Cash: ${cash:,.0f}  |  "
                     f"Today: {dpnl_emoji} {dpnlPct:+.2f}%  ({npos} pos)")
        for p in (alp.get("positions") or [])[:3]:
            upl  = float(p.get("unrealized_pl") or 0)
            uplp = float(p.get("unrealized_plpc") or 0) * 100
            e    = "🟢" if upl >= 0 else "🔴"
            lines.append(f"  {e} <b>{p['symbol']}</b>  {upl:+.0f}$ ({uplp:+.1f}%)")
        lines.append("")

    # ── Paper performance ──────────────────────────────────────────────
    perf = g("performance") or {}
    if perf.get("portfolio_value") is not None:
        port_r = perf.get("portfolio_return_pct", 0)
        spy_r  = perf.get("spy_return_pct", 0)
        alpha  = perf.get("alpha_pct", 0)
        n      = perf.get("n_trades", 0)
        a_emj  = "🟢" if alpha >= 0 else "🔴"
        lines.append(f"📈 Paper P&L: {port_r:+.1f}%  |  SPY: {spy_r:+.1f}%  |  Alpha: {a_emj} {alpha:+.2f}%  ({n} trades)")
        lines.append("")

    # ── Cross-repo check ───────────────────────────────────────────────
    repo = g("repo_detail") or {}
    ag   = (repo.get("agreement") or {})
    if ag.get("total", 0) > 0:
        lines.append(f"🔗 Cross-repo: {ag.get('agree',0)}/{ag.get('total',0)} systems agree")
        lines.append("")

    # ── Data freshness ─────────────────────────────────────────────────
    fr = g("freshness") or {}
    if fr:
        rl_warn = "" if fr.get("rl_source") == "LIVE" else "  ⚠️ RL IS STUB"
        lines.append(f"🕐 RL:{fr.get('rl_source')}{rl_warn}  Data:{fr.get('market_data')}  News:{fr.get('news')}")
        lines.append(f"   Generated: {fr.get('generated_utc','—')}")
        lines.append("")

    # ── Plain English Summary ──────────────────────────────────────────
    lines.append(_plain_english_summary(b))

    # ── Commands ───────────────────────────────────────────────────────
    lines.append("<b>Commands:</b> <code>BUY 1</code> <code>BUY XLF</code> <code>SKIP</code> <code>PORTFOLIO</code>")
    lines.append("<code>CRYPTO</code> <code>GOLD</code> <code>ALPHA</code> <code>BALANCE 12500</code> <code>EXPLAIN XLF</code>")
    lines.append("Or ask any question in plain English.")
    return "\n".join(lines)


def _format_opp(opp, label) -> str:
    """Format a single ranked opportunity for Telegram HTML. label is 'A','B','C'... or int."""
    # opp is either an Opportunity object or a dict (from JSON serialization)
    if isinstance(opp, dict):
        ticker           = opp.get("ticker", "?")
        name             = opp.get("name", "?")
        conviction       = opp.get("conviction", "LOW")
        rationale        = opp.get("rationale", [])
        suggested_pct    = opp.get("suggested_pct", 0)
        suggested_dollar = opp.get("suggested_dollar")
        asset_type       = opp.get("asset_type", "sector")
    else:
        ticker           = opp.ticker
        name             = opp.name
        conviction       = opp.conviction
        rationale        = opp.rationale
        suggested_pct    = opp.suggested_pct
        suggested_dollar = opp.suggested_dollar
        asset_type       = opp.asset_type

    c_emoji = {"HIGH": "🟢", "MEDIUM": "🟡", "LOW": "🔵",
                "SPECULATIVE": "⚡", "AVOID": "🔴"}.get(conviction, "⚪")
    c_label = {"HIGH": "Aggressive", "MEDIUM": "Balanced",
                "LOW": "Defensive", "SPECULATIVE": "Macro/Crypto"}.get(conviction, conviction)

    dollar_str  = f"  → <b>${suggested_dollar:,.0f}</b>" if suggested_dollar else ""
    kelly_pct   = opp.get("kelly_pct") if isinstance(opp, dict) else None
    kelly_str   = f"  Kelly: {kelly_pct:.0f}%" if kelly_pct else ""
    lines = [f"  <b>Option {label}</b> {c_emoji} [{c_label}] — <b>{ticker}</b> ({name})",
             f"     {suggested_pct:.0f}% of portfolio{dollar_str}{kelly_str}"]
    for r in (rationale or [])[:2]:
        lines.append(f"     • {r}")
    return "\n".join(lines)


# ── quick-reply formatters ────────────────────────────────────────────────

def format_crypto_briefing(signals: dict) -> str:
    """Compact crypto + macro briefing for the CRYPTO command."""
    if not signals:
        return "⚠️ Crypto data unavailable (network timeout)."
    lines = ["📊 <b>Crypto + Macro Live</b>", ""]

    order = ["crypto", "macro"]
    by_type = {t: [] for t in order}
    for sig in signals.values():
        by_type.get(sig.get("type", "macro"), by_type["macro"]).append(sig)

    type_labels = {"crypto": "⚡ Crypto (BTC/ETH — high-beta macro)",
                   "macro":  "🏦 Macro Hedges (GLD/TLT/QQQ)"}

    for t in order:
        group = by_type[t]
        if not group:
            continue
        lines.append(f"<b>{type_labels[t]}</b>")
        for s in group:
            chg  = s.get("change_24h_pct", 0)
            mom  = s.get("mom_7d_pct", 0)
            rsi  = s.get("rsi", 50)
            sig  = s.get("signal", "NEUTRAL")
            price = s.get("price", "?")
            arrow = "📈" if chg > 1.5 else "📉" if chg < -1.5 else "➡️"
            lines.append(f"  {arrow} <b>{s['name']}</b> ({s['ticker']})")
            price_fmt = f"{price:,.0f}" if isinstance(price, (int, float)) and price >= 10 else (f"{price:,.2f}" if isinstance(price, (int, float)) else str(price))
            lines.append(f"     Price: ${price_fmt}  |  24h: {chg:+.1f}%  |  7d: {mom:+.1f}%")
            lines.append(f"     RSI: {rsi}  |  Signal: {sig}  |  Max: {s.get('max_alloc_pct',0):.1f}%")
        lines.append("")

    lines.append("⚠️ Crypto = high volatility. BTC + ETH combined max 10% of portfolio.")
    lines.append("Reply <code>BALANCE 12500</code> to get dollar amounts.")
    return "\n".join(lines)


def format_portfolio(summary: dict) -> str:
    """Format real holdings summary for PORTFOLIO command."""
    if not summary or summary.get("balance") is None:
        return ("📂 <b>Portfolio</b>\n\nNo balance set yet.\n"
                "Reply <code>BALANCE 12500</code> to get started.")

    balance = summary["balance"]
    holdings = summary.get("holdings", [])
    total_invested = summary.get("total_invested", 0)
    unrealized = summary.get("unrealized_pnl", 0)
    unrealized_pct = summary.get("unrealized_pct", 0)
    cash = summary.get("cash_remaining", balance - total_invested)

    lines = [f"💼 <b>Portfolio  (Balance: ${balance:,.0f})</b>", ""]

    if not holdings:
        lines.append(f"No positions logged yet.")
        lines.append(f"Cash available: <b>${balance:,.0f}</b>")
        lines.append("\nLog a trade: <code>BOUGHT XLE 5 47.50</code>")
        return "\n".join(lines)

    for h in holdings:
        pnl_emoji = "🟢" if h.get("pnl_pct", 0) >= 0 else "🔴"
        ticker = h["ticker"]
        cost   = h.get("cost_basis", 0)
        curr   = h.get("current_value", cost)
        pct    = h.get("pnl_pct", 0)
        alloc  = h.get("alloc_pct", 0)
        price  = h.get("current_price")
        price_str = f" @ ${price:,.2f}" if price else ""
        lines.append(f"  {pnl_emoji} <b>{ticker}</b>{price_str}")
        lines.append(f"     ${cost:,.0f} → ${curr:,.0f}  ({pct:+.1f}%)  |  {alloc:.1f}% of portfolio")

    lines.append("")
    u_emoji = "🟢" if unrealized >= 0 else "🔴"
    lines.append(f"Unrealized P&L: {u_emoji} ${unrealized:+,.2f}  ({unrealized_pct:+.1f}%)")
    lines.append(f"Invested: ${total_invested:,.0f}  |  Cash: ${cash:,.0f}")
    lines.append("\n<code>BOUGHT TICKER SHARES PRICE</code> or <code>SOLD TICKER</code> to update.")
    return "\n".join(lines)


def format_sizing_guide(ticker: str, balance: float, conviction: str = "MEDIUM") -> str:
    """Answer 'how much should I put in X?' based on balance and conviction."""
    pct_map = {"HIGH": (20, 28), "MEDIUM": (10, 18), "LOW": (5, 9),
               "SPECULATIVE": (1, 3)}
    lo_pct, hi_pct = pct_map.get(conviction.upper(), (10, 18))
    lo_dollar = balance * lo_pct / 100
    hi_dollar = balance * hi_pct / 100
    desc = SECTOR_DESCRIPTIONS.get(ticker.upper(), "")
    lines = [f"💰 <b>Sizing Guide: {ticker.upper()}</b>",
             f"Your balance: ${balance:,.0f}  |  Conviction: {conviction}",
             "",
             f"Suggested range: <b>{lo_pct}-{hi_pct}% = ${lo_dollar:,.0f}-${hi_dollar:,.0f}</b>",
             ""]
    if desc:
        lines.append(f"What it is: {desc}")
    lines += ["",
              "Rules of thumb:",
              "  • Never put more than 30% in a single position",
              "  • Keep crypto (BTC + ETH combined) under 10% total",
              "  • Always keep ≥ 20% cash for opportunities"]
    return "\n".join(lines)


def format_explain(ticker: str) -> str:
    desc = SECTOR_DESCRIPTIONS.get(ticker.upper())
    if desc:
        return f"📖 <b>{ticker.upper()}</b>\n\n{desc}"
    return f"ℹ️ I don't have a description for {ticker.upper()}. Try: sector ETFs (XLK, XLF, XLE…), crypto (BTC-USD, ETH-USD), or macro hedges (GLD, TLT, QQQ)."


def answer_question(question: str, briefing: dict = None) -> str:
    """
    Best-effort natural language Q&A. Handles common questions the user might ask.
    Falls back to a helpful 'I don't know' with suggestions.
    """
    q = question.lower().strip()

    if any(w in q for w in ["what is", "what's", "explain", "tell me about"]):
        for ticker in SECTOR_DESCRIPTIONS:
            if ticker.lower() in q:
                return format_explain(ticker)

    if any(w in q for w in ["how much", "how many", "what size", "sizing", "position size"]):
        for ticker in SECTOR_DESCRIPTIONS:
            if ticker.lower() in q:
                return (f"Reply <code>HOW MUCH {ticker}</code> for a sizing guide. "
                        f"First set your balance: <code>BALANCE 12500</code>")
        return ("Reply <code>HOW MUCH XLE</code> (or any ticker) for a sizing guide.\n"
                "First set your balance: <code>BALANCE 12500</code>")

    if any(w in q for w in ["buy", "should i", "good time", "entry"]):
        if briefing:
            ranked = briefing.get("ranked_opportunities", [])
            if ranked:
                top = ranked[0]
                ticker = top.get("ticker") if isinstance(top, dict) else top.ticker
                return (f"Based on today's signals, top pick is <b>{ticker}</b>.\n"
                        f"Reply <code>BUY {ticker}</code> to log it, or <code>BUY 1</code> for the #1 pick.")
        return "Reply <code>STATUS</code> for today's recommendation."

    if any(w in q for w in ["vix", "volatility", "market stress"]):
        if briefing:
            vix = briefing.get("vix")
            regime = briefing.get("regime")
            if vix:
                guidance = ("Normal range — proceed carefully." if vix < 20
                            else "Elevated — reduce position sizes." if vix < 30
                            else "CRISIS level — consider staying in cash (BIL).")
                return f"VIX is currently <b>{vix}</b> ({regime}).\n{guidance}"
        return "VIX data not available. Reply <code>STATUS</code> for current market state."

    if any(w in q for w in ["portfolio", "holdings", "position"]):
        return "Reply <code>PORTFOLIO</code> to see your holdings and P&L."

    if any(w in q for w in ["crypto", "bitcoin", "btc", "ethereum", "eth"]):
        return "Reply <code>CRYPTO</code> for a live crypto + macro briefing."

    if any(w in q for w in ["gold", "gld", "hedge", "inflation"]):
        return "Reply <code>GOLD</code> for gold and macro hedge signals."

    if any(w in q for w in ["sell", "exit", "take profit", "stop loss"]):
        return ("I can log a sell for you — reply <code>SOLD XLE</code> (replace XLE with your ticker).\n"
                "For paper trades, use <code>SELL XLE</code>.")

    if any(w in q for w in ["performance", "p&l", "return", "alpha"]):
        return "Reply <code>PERF</code> for full performance summary."

    # Generic fallback
    return ("I'm not sure what you're asking. Try:\n"
            "• <code>STATUS</code> — today's recommendation\n"
            "• <code>PORTFOLIO</code> — your holdings\n"
            "• <code>CRYPTO</code> — crypto signals\n"
            "• <code>EXPLAIN XLF</code> — learn about any ticker\n"
            "• <code>BALANCE 12500</code> — set your portfolio size\n"
            "• <code>WHY</code> — why the RL made its pick")


def _plain_english_summary(b: dict) -> str:
    """Plain English TL;DR appended to every briefing."""
    g          = b.get
    vix        = g("vix") or 16
    ranked     = g("ranked_opportunities") or []
    abstain    = g("abstain_reason") or ""
    macro      = g("macro") or {}
    yc         = macro.get("yield_curve") or {}
    port       = g("portfolio_snap") or {}
    ticker     = g("ticker") or "SPY"
    action     = g("action") or "BUY"
    confidence = int(g("confidence") or 50)

    _sector_names = {
        "XLF": "Financials", "XLK": "Technology", "XLE": "Energy",
        "XLV": "Health Care", "XLY": "Consumer Disc.", "XLP": "Consumer Staples",
        "XLI": "Industrials", "XLB": "Materials", "XLRE": "Real Estate",
        "XLU": "Utilities", "XLC": "Comm. Services",
        "SPY": "S&P 500 broad", "QQQ": "Nasdaq 100", "BIL": "T-Bills (cash)",
        "TLT": "Long Bonds", "GLD": "Gold", "BTC-USD": "Bitcoin",
    }

    lines = ["", "─────────────────────────────",
             "📋 <b>PLAIN ENGLISH</b>", ""]

    # ── Portfolio snapshot ─────────────────────────────────────────────
    bal       = port.get("balance")
    invested  = port.get("total_invested") or 0
    cash_left = port.get("cash_remaining")
    holdings  = port.get("holdings") or []
    if bal:
        held_str = ""
        if holdings:
            parts = [f"${h['cost_basis']:,.0f} in {h['ticker']}" for h in holdings]
            held_str = " (" + ", ".join(parts) + ")"
        cash_str = f"${cash_left:,.0f} cash left" if cash_left is not None else ""
        port_line = f"💼 <b>Your money:</b> ${bal:,.0f} total"
        if invested:
            port_line += f" · ${invested:,.0f} invested{held_str}"
        if cash_str:
            port_line += f" · {cash_str}"
        lines.append(port_line)
        lines.append("")

    # ── Market conditions ──────────────────────────────────────────────
    try:
        vix_f = float(vix)
    except Exception:
        vix_f = 16.0
    vts_data  = (b.get("macro") or {}).get("vix_term_structure", {})
    ts_regime = vts_data.get("ts_regime", "")
    if ts_regime == "BACKWARDATION":
        market_line = "🔴 FEAR SPIKE — VIX term structure inverted, high stress"
    elif vix_f < 15 or ts_regime == "STEEP_CONTANGO":
        market_line = "🟢 Calm — good day to consider new positions"
    elif vix_f < 22:
        market_line = "🟡 Normal — nothing unusual, proceed as planned"
    elif vix_f < 30:
        market_line = "🟠 Choppy — keep sizes smaller than usual"
    else:
        market_line = "🔴 STRESSED — hold extra cash, be very careful"
    lines.append(f"📊 <b>Market:</b> {market_line}  (VIX {vix_f:.1f})")
    lines.append("")

    # ── Confidence label (Cameron's scale: 30=maybe, 70=definitely, 100=must) ──
    def _conf_label(pct):
        pct = int(pct)
        if pct >= 80:
            return f"🔥 {pct}% — MUST BUY — very high conviction"
        elif pct >= 65:
            return f"✅ {pct}% — DEFINITELY — strong signal, buy it"
        elif pct >= 50:
            return f"🟡 {pct}% — PROBABLY — solid signal, consider a normal position"
        elif pct >= 35:
            return f"⚠️ {pct}% — MAYBE — weak signal, small position or wait"
        else:
            return f"❌ {pct}% — SKIP — confidence too low to act"

    # ── Sizing helper ──────────────────────────────────────────────────
    top_op  = ranked[0] if ranked and isinstance(ranked[0], dict) else {}
    pct_sz  = top_op.get("suggested_pct") or 9
    dollar  = top_op.get("suggested_dollar")
    if not dollar and bal:
        dollar = int(bal * pct_sz / 100)
    size_str = f"${dollar:,.0f}" if dollar else f"~{pct_sz:.0f}% of your balance"

    # ── Determine call type and render ─────────────────────────────────
    is_crisis  = ticker == "BIL"
    is_abstain = ticker == "SPY" and bool(abstain)
    sector_name = _sector_names.get(ticker, ticker)

    if is_crisis:
        lines.append("⛔ <b>NO TRADE — CRISIS MODE</b>")
        lines.append(f"VIX is dangerously high ({vix_f:.0f}). System forces you to cash.")
        lines.append("")
        lines.append(
            "✅ <b>Your move:</b> Stay in cash.\n"
            "→ Reply <code>SKIP</code> to log no action today"
        )

    elif is_abstain:
        lines.append("⚠️ <b>NO SECTOR CALL TODAY</b>")
        lines.append("The RL models have no strong sector conviction right now.")
        lines.append(f"📊 Confidence: {_conf_label(confidence)}")
        lines.append("")
        if ranked:
            alt    = ranked[0] if isinstance(ranked[0], dict) else {}
            alt_t  = alt.get("ticker", "SPY")
            alt_nm = _sector_names.get(alt_t, alt_t)
            lines.append(f"📌 <b>Best ranked pick:</b> {alt_t} ({alt_nm})")
            lines.append("")
        lines.append(
            "✅ <b>Your move:</b>\n"
            "→ Reply <code>SKIP</code> — recommended today\n"
            "→ Or <code>BUY SPY</code> for a small broad-market position"
        )

    else:
        # Normal sector recommendation — show it clearly
        lines.append(f"🎯 <b>TODAY'S CALL: {action} {ticker} ({sector_name})</b>")
        lines.append(f"📊 Confidence: {_conf_label(confidence)}")
        lines.append(f"💰 Suggested size: <b>{size_str}</b>")
        lines.append("")

        if confidence >= 50:
            lines.append(
                f"✅ <b>Your move:</b>\n"
                f"→ Reply <code>BUY A</code> or <code>BUY {ticker}</code> to log it\n"
                f"→ Or <code>SKIP</code> to pass today"
            )
        elif confidence >= 35:
            lines.append(
                f"✅ <b>Your move:</b>\n"
                f"→ Reply <code>SKIP</code> — low signal, lean toward waiting\n"
                f"→ Or <code>BUY A</code> for a small position ({size_str}) if you want exposure"
            )
        else:
            lines.append(
                f"✅ <b>Your move:</b>\n"
                f"→ Reply <code>SKIP</code> — confidence too low to act"
            )

    # ── Equity alpha debrief ───────────────────────────────────────────
    equity_picks = b.get("equity_alpha_picks") or []
    if equity_picks:
        lines.append("")
        lines.append("📈 <b>STOCK PICKS — should you buy?</b>")
        conv_call = {"HIGH": "✅ BUY", "MEDIUM": "🟡 CONSIDER", "LOW": "⏭️ SKIP", "AVOID": "🔴 AVOID"}
        conv_desc = {"HIGH": "HIGH conviction", "MEDIUM": "MEDIUM conviction",
                     "LOW": "LOW conviction — skip unless you have reason", "AVOID": "avoid"}
        for p in equity_picks[:3]:
            ticker_p = p.get("ticker", "?")
            conv     = p.get("conviction", "LOW")
            call     = conv_call.get(conv, "⏭️ SKIP")
            desc     = conv_desc.get(conv, conv)
            dollar   = p.get("suggested_dollar")
            tagline  = p.get("conviction_tagline", "")
            size_str = f" → <b>${dollar:,.0f}</b>" if dollar else ""
            lines.append(f"  {call}: <b>{ticker_p}</b>  [{desc}]{size_str}")
            if tagline:
                lines.append(f"     💡 {tagline}")
            if conv in ("HIGH", "MEDIUM"):
                lines.append(f"     → Reply <code>BUY {ticker_p}</code> to log it")
        lines.append("")

    # ── Macro flag ─────────────────────────────────────────────────────
    if yc:
        try:
            inverted = yc.get("inverted", False)
            spread   = float(str(yc.get("spread", 1)).replace("%", ""))
            if inverted:
                lines.append(
                    "\n⚠️ <b>Macro flag:</b> Yield curve is inverted — historically a recession "
                    "warning. Don't make big new bets until this clears."
                )
            elif spread < 0.5:
                lines.append("\n📉 <b>Macro note:</b> Yield curve nearly flat — watch for inversion.")
        except Exception:
            pass

    lines.append(
        "\n💬 Ask me anything: \"should I sell XLE?\", \"what is XLF?\", "
        "\"is this a good time to buy crypto?\" — Gemini answers with today's live data."
    )

    return "\n".join(lines)


# ── command parser ────────────────────────────────────────────────────────

KNOWN_COMMANDS = {
    "BUY", "SELL", "SKIP", "HOLD",
    "STATUS", "WHY", "PERF", "RISK", "REPORT",
    "CRYPTO", "GOLD", "PORTFOLIO", "ALPHA",
    "BALANCE", "BOUGHT", "SOLD",
    "EXPLAIN", "HOW",
}


def parse_command(text: str) -> dict:
    """
    Parse an inbound Telegram message into a structured command.
    Returns {command, ticker, amount, reason, raw}.
    """
    if not text:
        return {"command": "UNKNOWN", "ticker": None, "reason": "", "raw": ""}

    raw    = text.strip()
    tokens = raw.replace("-", " ").split() if False else raw.split()  # keep hyphens for BTC-USD
    if not tokens:
        return {"command": "UNKNOWN", "ticker": None, "reason": "", "raw": raw}

    cmd_word = tokens[0].upper()

    # natural language question detection
    if cmd_word not in KNOWN_COMMANDS and "?" in raw:
        return {"command": "QUESTION", "ticker": None, "reason": raw, "raw": raw}
    if cmd_word not in KNOWN_COMMANDS and len(tokens) > 2:
        return {"command": "QUESTION", "ticker": None, "reason": raw, "raw": raw}

    # HOW MUCH handling — only if it's exactly "HOW MUCH <TICKER>" with a valid ticker
    if cmd_word == "HOW" and len(tokens) > 1 and tokens[1].upper() == "MUCH":
        t3 = tokens[2].upper() if len(tokens) > 2 else None
        # Only treat as HOW_MUCH command if third token looks like a ticker
        if t3 and ((t3.isalpha() and 2 <= len(t3) <= 5) or ("-" in t3 and len(t3) <= 10)):
            return {"command": "HOW_MUCH", "ticker": t3, "reason": "", "raw": raw}
        # Otherwise it's a natural language question
        return {"command": "QUESTION", "ticker": None, "reason": raw, "raw": raw}

    if cmd_word not in KNOWN_COMMANDS:
        # Route anything unrecognized to Gemini rather than showing command list
        return {"command": "QUESTION", "ticker": None, "reason": raw, "raw": raw}

    ticker = None
    amount = None
    reason_start = 1

    # SKIP/HOLD never have a ticker — everything after the command word is the reason
    if cmd_word in ("SKIP", "HOLD"):
        reason = " ".join(tokens[1:]).strip()
        return {"command": cmd_word, "ticker": None, "amount": None,
                "reason": reason, "raw": raw}

    # Ticker detection: token looks like XLE, BTC-USD, GLD, etc.
    if len(tokens) > 1:
        t2 = tokens[1].upper()
        # BUY 1 / BUY 2 — pick by rank number
        if t2.isdigit():
            amount = int(t2)
            reason_start = 2
        # BUY A / BUY B / BUY C — pick by letter (Option A, B, C)
        elif t2 in ("A", "B", "C", "D", "E"):
            amount = ord(t2) - ord("A") + 1   # A→1, B→2, C→3
            reason_start = 2
        # Ticker: 2-5 alpha chars OR contains hyphen (crypto like BTC-USD)
        elif (t2.isalpha() and 2 <= len(t2) <= 5) or ("-" in t2 and len(t2) <= 10):
            ticker = t2
            reason_start = 2

    # BALANCE command — amount is the whole second token
    if cmd_word == "BALANCE" and len(tokens) > 1:
        try:
            amount = float(tokens[1].replace("$", "").replace(",", ""))
            ticker = None
        except ValueError:
            pass
        return {"command": "BALANCE", "ticker": None, "amount": amount, "reason": "", "raw": raw}

    # BOUGHT / SOLD — pass full tokens for position_tracker.parse_bought_command
    if cmd_word in ("BOUGHT", "SOLD"):
        return {"command": cmd_word, "ticker": ticker,
                "tokens": tokens, "reason": "", "raw": raw}

    reason = " ".join(tokens[reason_start:]).lstrip("-: ").strip()
    return {
        "command": cmd_word,
        "ticker": ticker,
        "amount": amount,
        "reason": reason,
        "raw": raw,
    }


# ── shared helpers ────────────────────────────────────────────────────────

def _chunk(text, size=4000):
    if len(text) <= size:
        return [text]
    chunks, current = [], ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > size:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks


if __name__ == "__main__":
    demo = {
        "date": "2026-05-25", "regime": "NORMAL", "vix": 18.5,
        "ticker": "XLF", "action": "BUY", "confidence": 78,
        "rl_votes": {"PPO": "BUY XLF", "A2C": "BUY XLF", "SAC": "HOLD"},
        "news_sentiment": 0.38, "news_headline": "Bank earnings beat",
        "rsi": 44, "rel_strength": 1.2,
        "ranked_opportunities": [
            {"ticker": "XLF", "name": "Financials", "conviction": "HIGH",
             "suggested_pct": 25, "suggested_dollar": 3125, "asset_type": "sector",
             "rationale": ["RL ensemble pick (78% confidence)", "algo-system eligible (+0.45)",
                           "outperforming SPY +1.8%"]},
            {"ticker": "XLK", "name": "Technology", "conviction": "MEDIUM",
             "suggested_pct": 15, "suggested_dollar": 1875, "asset_type": "sector",
             "rationale": ["momentum +2.4%", "news bullish (+0.31)"]},
            {"ticker": "GLD", "name": "Gold ETF", "conviction": "LOW",
             "suggested_pct": 7, "suggested_dollar": 875, "asset_type": "macro",
             "rationale": ["inflation hedge", "mild bullish momentum"]},
        ],
        "performance": {"portfolio_return_pct": 4.2, "spy_return_pct": 2.1,
                        "alpha_pct": 2.1, "n_trades": 7, "portfolio_value": 10500},
        "freshness": {"rl_source": "LIVE", "market_data": "LIVE",
                      "news": "LEXICON", "generated_utc": "2026-05-25 13:00 UTC"},
    }
    print(format_briefing(demo))
    print("\n--- parse tests ---")
    tests = ["BUY XLF - strong", "BUY 1", "SKIP not convinced", "STATUS",
             "BOUGHT XLE 5 47.50", "BOUGHT BTC-USD 500", "SOLD XLE",
             "BALANCE 12500", "EXPLAIN XLF", "HOW MUCH XLE",
             "what is XLF?", "how much should I put in Bitcoin?", "should I buy XLK?"]
    for t in tests:
        print(f"  {t!r} → {parse_command(t)}")
