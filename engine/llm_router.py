"""
llm_router.py — Gemini conversational AI for Sector Command

Routes natural-language questions through Google Gemini 2.0 Flash.
The model sees the full market context (regime, VIX, RL signal, ranked picks,
portfolio) and responds like a personal quant analyst.

Setup (one-time, 2 minutes):
  1. Go to https://aistudio.google.com/app/apikey
  2. Click "Create API key" (free — no credit card required)
  3. Add it as a GitHub Secret and Vercel env var named GEMINI_API_KEY
  4. Also export it locally: export GEMINI_API_KEY="AIza..."

The router degrades gracefully: if no key is set, it returns a helpful
fallback response pointing Cameron to the command menu.
"""

import os
import json
import datetime

# Models tried in order; first 200-response wins (avoids 429 quota exhaustion)
MODELS = [
    "gemini-2.0-flash-lite",   # free-tier quota typically available
    "gemini-2.5-flash",        # newer, fallback
    "gemini-2.0-flash",        # may hit quota limit:0 on free plan
    "gemini-flash-latest",     # alias
]

SYSTEM_PROMPT = """You are Sector Command, a personal quant trading assistant built by Cameron Camarotti.

Cameron is a high school student learning to invest. Talk to him like a smart older friend who knows finance — clear, direct, no jargon without explanation.

Your system architecture (explain this when relevant):
- 3 RL agents: PPO, A2C, SAC — each trained on 11 sector ETFs. They vote. If 2+ agree, that is the signal.
- Governance: if VIX goes above 35, everything moves to BIL (safe cash ETF) automatically
- News sentiment: AI scans 7 news feeds and scores them bullish/bearish
- Universe: 11 sector ETFs (XLF, XLE, XLK, etc.) + crypto (BTC, ETH) + hedges (GLD, TLT)

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
  Give a CLEAR recommendation. Use your own knowledge of the tickers + the market context data provided. Say:
  - The top pick from the system and why (sector fundamentals, momentum, sentiment)
  - How much to put in based on their balance
  - What to reply to log it

If asked about a specific ticker (like "what is XLF" or "tell me about XLE"):
  Explain what the ETF holds, why it does well in current conditions, and whether the system likes it

If asked about next briefing timing:
  Briefings run at 9am, 12pm, 3pm, 6pm Eastern on weekdays via GitHub Actions

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

    payload = {
        "contents": [{"parts": [{"text": full_prompt}]}],
        "generationConfig": {"temperature": 0.4, "maxOutputTokens": 900},
    }

    last_err = None
    for model in MODELS:
        try:
            url = (f"https://generativelanguage.googleapis.com/v1beta/models/{model}"
                   f":generateContent?key={GEMINI_API_KEY}")
            r = _requests.post(url, json=payload, timeout=30)
            if r.status_code == 429:
                print(f"[llm_router] {model} quota exceeded, trying next model")
                last_err = f"{model} quota exceeded"
                continue
            r.raise_for_status()
            data = r.json()
            return data["candidates"][0]["content"]["parts"][0]["text"].strip()
        except Exception as e:
            print(f"[llm_router] {model} failed: {e}")
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


def load_market_context_from_disk() -> dict:
    """
    Load market context from data/last_briefing.json so the webhook
    can pass live data into the LLM without re-running all feeders.
    """
    path = os.path.join(os.path.dirname(__file__), "..", "data", "last_briefing.json")
    try:
        if os.path.exists(path):
            with open(path) as f:
                b = json.load(f)
            # Pull rl_signal fields up to the top level for convenience
            return {
                "vix":                 b.get("vix"),
                "regime":              b.get("regime"),
                "abstain_reason":      b.get("abstain_reason"),
                "news_sentiment":      b.get("news_sentiment"),
                "news_headline":       b.get("news_headline"),
                "ranked_opportunities": b.get("ranked_opportunities", []),
                "fomc_live":           b.get("fomc_live"),
                "rl_signal": {
                    "target":     b.get("ticker"),
                    "action":     b.get("action"),
                    "confidence": b.get("confidence"),
                    "votes":      b.get("rl_votes"),
                },
            }
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
