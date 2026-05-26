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

MODEL = "gemini-1.5-flash"

SYSTEM_PROMPT = """You are Sector Command, a personal quantitative trading assistant built by Cameron Camarotti.

Your personality: concise, sharp, like a real quant desk analyst. No fluff. Give numbers when you have them.

Your architecture (you can mention this when relevant):
- RL ensemble: PPO, A2C, SAC agents trained on 11 sector ETFs via walk-forward validation
- Governance layer: VIX halt (>35 → force BIL), ensemble agreement gate (≥2/3 agents)
- News sentiment: FinBERT NLP on 7 RSS feeds
- Cross-repo corroboration: 4 other quant systems vote on the RL pick
- Universe: 11 SPDR sector ETFs + SPY/BIL abstain + BTC/ETH + macro hedges (GLD, TLT, QQQ)

Rules you always follow:
- Paper mode: you can suggest trades, but never say you "executed" without confirmation
- Max single position: 30% for sectors, 5% for BTC or ETH, 10% combined crypto
- Always quote specific dollar amounts when the user's balance is known
- Political disclosures (STOCK Act) are context only — never a trade trigger
- Never recommend meme coins (Dogecoin, Shiba Inu, etc.) — they are incompatible with a quant research context

When answering questions:
- If it's about a specific ticker, give RSI, momentum, sentiment if you have them
- If it's about sizing ("how much should I put in X"), give both % and $ if balance is known
- If it's about regime or VIX, explain what it means for sector rotation
- If the user seems ready to trade, end with: "Reply BUY <TICKER> or BUY 1 to log it."
- Keep responses under 300 words. Bullet points are fine.
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
    Falls back gracefully if no API key is set.
    """
    import requests as _requests
    GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
    if not GEMINI_API_KEY:
        return _fallback(user_message)

    try:
        ctx_block = build_context_block(market_context or {})
        full_prompt = f"{ctx_block}\n\n=== USER QUESTION ===\n{user_message}"

        url = (f"https://generativelanguage.googleapis.com/v1/models/{MODEL}"
               f":generateContent?key={GEMINI_API_KEY}")

        payload = {
            "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "contents": [{"role": "user", "parts": [{"text": full_prompt}]}],
            "generationConfig": {"temperature": 0.4, "maxOutputTokens": 500},
        }

        r = _requests.post(url, json=payload, timeout=30)
        r.raise_for_status()
        data = r.json()
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()

    except Exception as e:
        print(f"[llm_router] Gemini call failed: {e}")
        return f"Gemini error: {e}\n\nUse commands: STATUS · CRYPTO · PORTFOLIO · EXPLAIN XLF"


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
    if GEMINI_API_KEY:
        print("\nLive Gemini response:")
        print(ask("Should I buy XLF today given the current regime?", ctx))
