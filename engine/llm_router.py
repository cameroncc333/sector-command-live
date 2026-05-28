"""
llm_router.py — Gemini conversational AI for Sector Command

Routes natural-language questions through Google Gemini 2.0 Flash.
The model sees the full market context (regime, VIX, RL signal, ranked picks,
portfolio) and responds like a personal quant analyst.

Setup (one-time, 2 minutes):
  1. Go to https://aistudio.google.com/app/apikey
  2. Click "Create API key" (free — no credit card required)
  3. Add it as a GitHub Secret and Railway env var named GEMINI_API_KEY
  4. Also export it locally: export GEMINI_API_KEY="AIza..."

The router degrades gracefully: if no key is set, it returns a helpful
fallback response pointing Cameron to the command menu.
"""

import os
import json
import datetime

# Models tried in order; first 200-response wins (avoids 429 quota exhaustion)
MODELS = [
    "gemini-2.5-flash",
    "gemini-2.0-flash-lite",
    "gemini-2.0-flash",
    "gemini-2.5-pro",
    "gemini-flash-latest",
    "gemini-pro-latest",
]

SYSTEM_PROMPT = """You are Sector Command, a personal quant trading assistant built by Cameron Camarotti.

Cameron is a high school student learning to invest. Talk to him like a smart older friend who knows finance — clear, direct, no jargon without explanation.

Your system architecture (explain this when relevant):
- 3 RL agents: PPO, A2C, SAC — each trained on 11 sector ETFs. They each vote for a sector. Even if they pick 3 different sectors, the system still acts — a lone minority vote lowers confidence by 10pts. All 3 agreeing boosts it +5pts.
- Governance hard rules: VIX ≥ 35 → force BIL (cash). All agents HOLD → abstain to SPY.
- News sentiment: FinBERT scores 7 live RSS feeds — bullish/bearish by sector
- Cross-repo corroboration: 3 other quant repos (equity-sector-analyzer, algo-trading-system, fed-rate-sector-analysis) each cast a vote. 0/3 agree = −12 confidence. The RL still acts unless overridden by hard governance rules.
- Individual stock picks: a cross-sectional factor model (value, quality, momentum, technical, low-vol) ranks ~80 stocks across all sectors. Conviction levels: HIGH 🔥, MEDIUM ✅, LOW 🟡, AVOID 🔴
- Sell signals: trailing stop (−5% from entry high), RSI overbought (>72), momentum flip, stale loss (>45 days negative). Also conviction drops: HIGH→AVOID fires an exit alert.
- Universe: 11 sector ETFs + individual stocks (AAPL, MSFT, NVDA, XLF, etc.) + crypto (BTC, ETH) + hedges (GLD, TLT)
- Macro: yield curve (10yr−13w T-bill spread), DXY, VIX term structure (ratio>1 = fear, <0.85 = calm), Put/Call ratio

Rules:
- Paper mode — suggest trades but never say "executed"
- Max 30% in any one sector, max 5% per crypto, max 10% combined crypto
- Always give dollar amounts if balance is known
- Political disclosures are research context only, never a trade reason

HOW TO ANSWER:

If asked "what does everything mean" or "explain" or "dumb it down":
  Walk through EACH piece of the market context in plain English:
  1. VIX — explain what the number means (low = calm, high = fear), what NORMAL/CAUTION/RISK-OFF means for investing
  2. RL agents — say what PPO, A2C, SAC each voted for and what the combined pick is
  3. News sentiment — say if news is positive or negative and why it matters
  4. Top ranked picks — say what they are and why the system likes them
  5. What to do — give a clear action recommendation with dollar amounts

If asked "what should I invest in" or "what do you think":
  Give a CLEAR recommendation. Combine the system data above with your knowledge of the tickers and current market conditions. Say:
  - The top pick and why (reference the RL pick, news sentiment, AND your knowledge of that sector right now)
  - How much to put in based on their balance (give a dollar amount)
  - End with: "Reply BUY <TICKER> to log it."

If asked about a specific ticker (like "what is XLF" or "tell me about XLE"):
  Explain what the ETF holds, why it does well in current conditions, and whether the system likes it

If asked about next briefing timing:
  Briefings run at 9:00am, 10:30am, 12:00pm, 2:00pm, 3:30pm, 4:30pm Eastern on weekdays (6×/day via GitHub Actions)

End with "Reply BUY <TICKER> to log it." only when giving a trade recommendation.

FORMATTING RULES (CRITICAL — this is Telegram, plain text only):
- NO asterisks (**bold** or *italic*) — just write the word normally
- NO underscores for italic
- NO backticks or code blocks
- Bullet points: use a plain dash "- "
- Emphasis: use ALL CAPS sparingly
- Dollar signs and numbers are fine
- Keep it under 400 words total
"""


def build_context_block(market_context: dict) -> str:
    """
    Serialize the current market state into a compact string for the LLM prompt.
    """
    lines = [f"=== CURRENT MARKET STATE ({datetime.date.today()}) ==="]

    vix    = market_context.get("vix", "?")
    regime = market_context.get("regime", "?")
    lines.append(f"VIX: {vix}  |  Regime: {regime}")

    rl = market_context.get("rl_signal", {})
    if rl:
        lines.append(f"RL pick: {rl.get('action','?')} {rl.get('target','?')}  confidence: {rl.get('confidence','?')}%")
        votes = rl.get("votes") or {}
        if votes:
            lines.append("Agent votes: " + ", ".join(f"{k}:{v}" for k, v in votes.items()))

    abstain = market_context.get("abstain_reason")
    if abstain:
        lines.append(f"Governance override: {abstain}")

    ns = market_context.get("news_sentiment")
    if ns is not None:
        label = "Bullish" if ns > 0.15 else "Bearish" if ns < -0.15 else "Neutral"
        lines.append(f"News sentiment: {ns:+.3f} ({label})")

    hl = market_context.get("news_headline")
    if hl:
        lines.append(f"Top headline: {hl}")

    ranked = market_context.get("ranked_opportunities") or []
    if ranked:
        lines.append("\nToday's ranked picks:")
        labels = ["A", "B", "C", "D", "E"]
        for i, opp in enumerate(ranked[:5]):
            t = opp.get("ticker") if isinstance(opp, dict) else opp.ticker
            c = opp.get("conviction") if isinstance(opp, dict) else opp.conviction
            pct = opp.get("suggested_pct") if isinstance(opp, dict) else opp.suggested_pct
            dollar = opp.get("suggested_dollar") if isinstance(opp, dict) else opp.suggested_dollar
            reasons = (opp.get("rationale") if isinstance(opp, dict) else opp.rationale) or []
            dollar_str = f" ≈ ${dollar:,.0f}" if dollar else ""
            lines.append(f"  Option {labels[i]}: {t} [{c}] {pct:.0f}%{dollar_str} — {'; '.join(reasons[:2])}")

    portfolio = market_context.get("portfolio_summary") or {}
    balance = portfolio.get("balance") or market_context.get("balance")
    if balance:
        lines.append(f"\nUser's portfolio balance: ${balance:,.0f}")
        holdings = portfolio.get("holdings") or []
        if holdings:
            lines.append("Current holdings:")
            for h in holdings:
                ticker = h.get("ticker","?")
                val    = h.get("current_value", h.get("cost_basis", 0))
                pnl    = h.get("pnl_pct", 0)
                alloc  = h.get("alloc_pct") or 0
                lines.append(f"  {ticker}: ${val:,.0f}  ({pnl:+.1f}%)  {alloc:.1f}% of portfolio")
            cash = portfolio.get("cash_remaining")
            if cash is not None:
                lines.append(f"Cash available: ${cash:,.0f}")
        else:
            lines.append("No positions logged yet.")

    fomc = market_context.get("fomc_live") or {}
    if fomc.get("active"):
        lines.append(f"\nFOMC meeting window: {fomc.get('meeting_date','')} — Fed sentiment: {fomc.get('label','?')}")

    # Cross-repo corroboration
    corr = market_context.get("repo_corroboration") or {}
    algo_top = market_context.get("algo_top_pick")
    fed = market_context.get("fed_context") or {}
    fomc_sent = market_context.get("fomc_sentiment") or {}
    if corr or algo_top or fed:
        lines.append("\nCross-repo corroboration:")
        if corr:
            lines.append(f"  Repo agreement: {corr.get('agree',0)}/{corr.get('total',0)} systems support RL pick")
            for note in (corr.get("notes") or [])[:4]:
                lines.append(f"  - {note}")
        if algo_top:
            lines.append(f"  Algo-trading-system top pick: {algo_top}")
        if fed:
            lines.append(f"  Fed stance: {fed.get('stance','?')} @ {fed.get('rate','?')}%  "
                         f"Favored sectors: {', '.join(fed.get('favored',[]) or ['none'])}")
        if fomc_sent and fomc_sent.get("pmsi") is not None:
            pmsi = fomc_sent["pmsi"]
            label = "BULLISH" if pmsi > 0.15 else "BEARISH" if pmsi < -0.15 else "NEUTRAL"
            lines.append(f"  FOMC sentiment (PMSI): {pmsi:+.3f} [{label}]")

    # VIX term structure
    vts = market_context.get("vix_term_structure") or {}
    if vts.get("ratio"):
        lines.append(f"\nVIX term structure: ratio {vts['ratio']} [{vts.get('ts_regime','')}]  "
                     f"VIX {vts.get('vix','?')} / VIX3M {vts.get('vix3m','?')}")

    # Put/call ratio
    opts = market_context.get("options_signals") or {}
    spy_pcr = (opts.get("SPY") or opts.get("QQQ")) if opts else None
    if spy_pcr and spy_pcr.get("pcr"):
        lines.append(f"Put/Call ratio (SPY): {spy_pcr['pcr']} [{spy_pcr.get('signal','?')}] — "
                     f"{spy_pcr.get('interpretation','')}")

    # Yield curve + dollar
    yc  = market_context.get("yield_curve") or {}
    dxy = market_context.get("dollar") or {}
    if yc.get("spread") is not None:
        inv_str = " INVERTED — recession warning" if yc.get("inverted") else ""
        lines.append(f"Yield curve spread (10yr−13w): {yc['spread']}%{inv_str}")
    if dxy.get("dxy"):
        lines.append(f"Dollar index (DXY): {dxy['dxy']}  ({dxy.get('dxy_change',0):+.2f}%)  {dxy.get('signal','')}")

    # Social sentiment (StockTwits)
    social = market_context.get("social_sentiment") or {}
    if social:
        parts = []
        for tkr, s in list(social.items())[:5]:
            parts.append(f"{tkr}:{s.get('signal','?')}({s.get('score',0):+.2f})")
        lines.append(f"StockTwits social sentiment: {', '.join(parts)}")

    # Individual stock picks
    alpha_picks = market_context.get("equity_alpha_picks") or []
    if alpha_picks:
        lines.append("\nTop individual stock picks (cross-sectional factor model):")
        for p in alpha_picks[:5]:
            sc     = p.get("composite_score", 0)
            conv   = p.get("conviction", "")
            sector = p.get("sector_name", "")
            tag    = p.get("conviction_tagline", "")
            dollar = f" ≈ ${p['suggested_dollar']:.0f}" if p.get("suggested_dollar") else ""
            lines.append(f"  {p.get('ticker')} [{conv}] score {sc:.0f} ({sector}){dollar}"
                         + (f" — {tag}" if tag else ""))

    # Equity alpha exit signals
    alpha_exits = market_context.get("equity_alpha_exits") or []
    if alpha_exits:
        lines.append("\nEquity alpha exit signals:")
        for sig in alpha_exits:
            lines.append(f"  {sig.get('ticker')} [{sig.get('urgency')}] — {sig.get('detail','')}")

    # Sell signals for held positions
    sell_text = market_context.get("sell_alerts") or ""
    if sell_text:
        lines.append(f"\nSell signals for your holdings:\n{sell_text[:400]}")

    # Earnings warnings
    earn = market_context.get("earnings_warning") or ""
    if earn:
        lines.append(f"\nEarnings event risk:\n{earn[:300]}")

    # Paper performance
    perf = market_context.get("performance") or {}
    if perf.get("portfolio_value") is not None:
        lines.append(f"\nPaper portfolio performance:")
        lines.append(f"  Portfolio return: {perf.get('portfolio_return_pct',0):+.1f}%  "
                     f"SPY benchmark: {perf.get('spy_return_pct',0):+.1f}%  "
                     f"Alpha: {perf.get('alpha_pct',0):+.2f}%")
        lines.append(f"  Trades: {perf.get('n_trades',0)}  "
                     f"Win rate: {perf.get('win_rate') or 'N/A'}%  "
                     f"Running {perf.get('days_running',0)} days")

    # Crypto signals (top 3 only to save tokens)
    crypto = market_context.get("crypto_signals") or {}
    if crypto:
        crypto_parts = []
        for tkr, s in list(crypto.items())[:3]:
            crypto_parts.append(f"{tkr}:{s.get('signal','?')} ${s.get('price','?')}")
        lines.append(f"\nCrypto signals: {', '.join(crypto_parts)}")

    # Decision reasoning trace
    trace = market_context.get("why_trace") or []
    if trace:
        lines.append("\nDecision reasoning trace:")
        for t in trace[:10]:
            lines.append(f"  - {t}")

    return "\n".join(lines)


def ask(user_message: str, market_context: dict = None) -> str:
    """
    Route a natural-language message through Gemini REST API (no SDK dependency).
    Tries each model in MODELS order; skips 429/quota errors. Falls back if no key.
    """
    import requests as _requests
    GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
    if not GEMINI_API_KEY:
        return _fallback(user_message)

    ctx_block = build_context_block(market_context or {})
    full_prompt = (f"{SYSTEM_PROMPT}\n\n"
                   f"{ctx_block}\n\n"
                   f"=== USER QUESTION ===\n{user_message}")

    last_err = None
    for model in MODELS:
        # Disable thinking for 2.5 models so they respond like normal chat models
        thinking_off = model.startswith("gemini-2.5")
        payload = {
            "contents": [{"parts": [{"text": full_prompt}]}],
            "generationConfig": {
                "temperature": 0.4,
                "maxOutputTokens": 900,
                **({"thinkingConfig": {"thinkingBudget": 0}} if thinking_off else {}),
            },
        }
        try:
            url = (f"https://generativelanguage.googleapis.com/v1beta/models/{model}"
                   f":generateContent?key={GEMINI_API_KEY}")
            r = _requests.post(url, json=payload, timeout=30)
            if r.status_code == 429:
                print(f"[llm_router] {model} quota exceeded, trying next")
                last_err = f"{model} quota exceeded"
                continue
            if r.status_code >= 400:
                err_msg = r.json().get("error", {}).get("message", r.text[:80])
                print(f"[llm_router] {model} error {r.status_code}: {err_msg}")
                last_err = f"{model} {r.status_code}: {err_msg}"
                continue
            data = r.json()
            parts = data["candidates"][0]["content"]["parts"]
            # Skip thought parts (thinking models); take first non-thought text
            answer = next((p["text"] for p in parts if not p.get("thought")), parts[0]["text"])
            return answer.strip()
        except Exception as e:
            print(f"[llm_router] {model} exception: {e}")
            last_err = str(e)
            continue

    return f"AI unavailable ({last_err}).\n\nUse commands: STATUS · CRYPTO · PORTFOLIO · EXPLAIN XLF"


def _fallback(user_message: str) -> str:
    """Response when Gemini key isn't set yet."""
    q = user_message.lower()
    if any(w in q for w in ["crypto", "bitcoin", "btc", "eth"]):
        return "Reply CRYPTO for live BTC/ETH signals."
    if any(w in q for w in ["gold", "gld", "hedge"]):
        return "Reply GOLD for macro hedge signals."
    if any(w in q for w in ["portfolio", "holdings", "position"]):
        return "Reply PORTFOLIO to see your holdings and P&L."
    if any(w in q for w in ["why", "reason", "how did"]):
        return "Reply WHY for the full reasoning trace."
    if any(w in q for w in ["performance", "p&l", "return"]):
        return "Reply PERF for performance summary."
    return ("To enable full AI chat: add GEMINI_API_KEY to your environment (see setup.sh).\n"
            "Available commands: STATUS · WHY · PERF · CRYPTO · GOLD · PORTFOLIO · EXPLAIN XLF")


def _redis_get(key: str):
    """Fetch a JSON value from Upstash Redis REST API."""
    url   = os.environ.get("UPSTASH_REDIS_REST_URL", "")
    token = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
    if not url or not token:
        return None
    try:
        import requests as _req
        r = _req.post(url, json=["GET", key],
                      headers={"Authorization": f"Bearer {token}"}, timeout=3)
        raw = r.json().get("result")
        return json.loads(raw) if raw else None
    except Exception:
        return None


def _briefing_to_context(b: dict) -> dict:
    """Extract the full market context from a raw briefing dict for Gemini."""
    portfolio_snap = b.get("portfolio_snap") or {}
    balance = portfolio_snap.get("balance") or b.get("balance")
    repo = b.get("repo_detail") or {}
    macro = b.get("macro") or {}
    return {
        "vix":                  b.get("vix"),
        "regime":               b.get("regime"),
        "abstain_reason":       b.get("abstain_reason"),
        "news_sentiment":       b.get("news_sentiment"),
        "news_headline":        b.get("news_headline"),
        "ranked_opportunities": b.get("ranked_opportunities", []),
        "fomc_live":            b.get("fomc_live"),
        "balance":              balance,
        "portfolio_summary":    portfolio_snap,
        "rl_signal": {
            "target":     b.get("ticker"),
            "action":     b.get("action"),
            "confidence": b.get("confidence"),
            "votes":      b.get("rl_votes"),
        },
        # Cross-repo corroboration (equity-sector-analyzer, algo-trading-system, fed)
        "repo_corroboration":   repo.get("agreement"),
        "algo_top_pick":        repo.get("algo_top_pick"),
        "fed_context":          repo.get("fed_context"),
        "fomc_sentiment":       repo.get("fomc_sentiment"),
        # Macro indicators
        "yield_curve":          macro.get("yield_curve"),
        "dollar":               macro.get("dollar"),
        "vix_term_structure":   macro.get("vix_term_structure"),
        # Options & sentiment
        "options_signals":      b.get("options_signals"),
        "social_sentiment":     b.get("social_sentiment"),
        # Individual stock picks from cross-sectional factor model
        "equity_alpha_picks":   b.get("equity_alpha_picks", []),
        "equity_alpha_exits":   b.get("equity_alpha_exit_alerts", []),
        # Exit / risk signals
        "sell_alerts":          b.get("sell_alerts"),
        "earnings_warning":     b.get("earnings_warning"),
        # Paper performance
        "performance":          b.get("performance"),
        # Decision reasoning trace
        "why_trace":            b.get("why_trace", []),
        # Crypto signals
        "crypto_signals":       b.get("crypto_signals", {}),
    }


def load_market_context_from_disk() -> dict:
    """
    Load market context for Gemini — checks Redis first (always fresh on Railway),
    then falls back to data/last_briefing.json.
    """
    # Redis is the single source of truth on Railway
    cached = _redis_get("sc:last_briefing")
    if cached:
        return _briefing_to_context(cached)

    path = os.path.join(os.path.dirname(__file__), "..", "data", "last_briefing.json")
    try:
        if os.path.exists(path):
            with open(path) as f:
                b = json.load(f)
            return _briefing_to_context(b)
    except Exception as e:
        print(f"[llm_router] could not load market context: {e}")
    return {}


if __name__ == "__main__":
    # Quick smoke test (works without API key in fallback mode)
    ctx = {
        "vix": 18.5, "regime": "NORMAL",
        "news_sentiment": 0.28, "news_headline": "Bank earnings beat",
        "rl_signal": {"target": "XLF", "action": "BUY", "confidence": 78,
                      "votes": {"PPO": "BUY XLF", "A2C": "BUY XLF", "SAC": "HOLD"}},
        "ranked_opportunities": [
            {"ticker": "XLF", "name": "Financials", "conviction": "HIGH",
             "suggested_pct": 25, "suggested_dollar": 3125,
             "rationale": ["RL ensemble pick (78%)", "news bullish"]},
        ],
        "balance": 12500,
        "portfolio_summary": {"balance": 12500, "holdings": [], "cash_remaining": 12500},
    }
    print("Context block:\n")
    print(build_context_block(ctx))
    print("\nFallback response:")
    print(_fallback("should I buy gold today?"))
    if os.environ.get("GEMINI_API_KEY"):
        print("\nLive Gemini response:")
        print(ask("Should I buy XLF today given the current regime?", ctx))
