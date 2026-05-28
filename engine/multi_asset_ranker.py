"""
multi_asset_ranker.py — unified ranked opportunity list across all asset classes

Combines signals from:
  • RL ensemble (sectors)         → engine signal + confidence
  • Sector technicals              → RSI / momentum / Sharpe / rel-strength
  • Algo composite                 → z-scored factor score + golden-cross gate
  • Crypto/macro feeder            → BTC ETH (institutional crypto) + GLD TLT QQQ
  • News sentiment                 → per-ticker FinBERT score

Output: a ranked list of Opportunity dicts, each with:
  ticker, name, asset_type, conviction (HIGH/MEDIUM/LOW/SPECULATIVE/AVOID),
  score, rationale, suggested_pct, suggested_dollar (if balance known),
  signal_summary

Conviction → sizing rules (enforced here, never overridden upstream):
  HIGH          20-30%   (RL + algo agree, news positive, VIX normal)
  MEDIUM        10-20%   (partial agreement or mixed signals)
  LOW            5-10%   (single signal, low confidence)
  SPECULATIVE    3-5%    (BTC/ETH — high-beta uncorrelated macro, capped at 10% combined)
  AVOID          0%      (negative signals, governance block)

Max total crypto allocation: 10% of portfolio.
"""

from dataclasses import dataclass, field

SECTOR_NAMES = {
    "XLK": "Technology",       "XLF": "Financials",     "XLV": "Health Care",
    "XLY": "Consumer Discret.","XLP": "Consumer Staples","XLE": "Energy",
    "XLI": "Industrials",      "XLB": "Materials",       "XLRE": "Real Estate",
    "XLU": "Utilities",        "XLC": "Communication",
}
MACRO_NAMES = {"GLD": "Gold ETF", "TLT": "20Y Treasuries", "QQQ": "Nasdaq 100",
               "SPY": "S&P 500", "BIL": "Cash/T-Bills"}


@dataclass
class Opportunity:
    ticker:          str
    name:            str
    asset_type:      str     # sector / crypto / macro
    conviction:      str     # HIGH / MEDIUM / LOW / SPECULATIVE / AVOID
    score:           float   # composite 0-100
    rationale:       list    # list of short strings (shown in briefing)
    suggested_pct:   float   # % of portfolio
    suggested_dollar: float = None
    signal_summary:  dict    = field(default_factory=dict)


def rank(
    rl_signal:       dict,        # from load_rl_signal()
    sector_tech:     dict = None, # from repo_signals.sector_technicals()
    algo_signals:    dict = None, # from repo_signals.algo_composite_signal()
    crypto_signals:  dict = None, # from crypto_feeder.get_crypto_signals()
    news_by_sector:  dict = None, # {ticker: sentiment_float}
    vix:             float = 20.0,
    balance:         float = None,
    max_results:     int   = 5,
) -> list[Opportunity]:
    """
    Main entry. Returns up to max_results Opportunity objects, ranked by score desc.
    Never returns AVOID items.
    """
    opportunities = []

    # ── 1. Sector opportunities ──────────────────────────────────────────
    try:
        from engine.repo_signals import sector_technicals, algo_composite_signal
    except Exception as _e:
        print(f"[multi_asset_ranker] repo_signals unavailable: {_e}")
        sector_technicals = lambda: {}
        algo_composite_signal = lambda: {"by_sector": {}, "top": None}
    tech = sector_tech or sector_technicals()
    algo = algo_signals or algo_composite_signal()
    news = news_by_sector or {}

    rl_target     = rl_signal.get("target", "")
    rl_action     = rl_signal.get("action", "")
    rl_confidence = float(rl_signal.get("confidence", 50))

    for ticker, name in SECTOR_NAMES.items():
        t   = tech.get(ticker, {})
        a   = algo.get("by_sector", {}).get(ticker, {})
        ns  = news.get(ticker)
        opp = _score_sector(ticker, name, t, a, ns, rl_target, rl_action,
                            rl_confidence, vix)
        if opp and opp.conviction != "AVOID":
            opportunities.append(opp)

    # ── 2. Crypto + macro opportunities ─────────────────────────────────
    if crypto_signals:
        total_crypto_pct = 0.0
        for ticker, cs in crypto_signals.items():
            opp = _score_crypto(cs, vix, total_crypto_pct)
            if opp and opp.conviction not in ("AVOID",):
                if cs["type"] == "crypto":
                    total_crypto_pct += opp.suggested_pct
                opportunities.append(opp)

    # ── 3. Sort by score desc, keep top N ────────────────────────────────
    opportunities.sort(key=lambda o: o.score, reverse=True)
    top = opportunities[:max_results]

    # ── 4. Attach dollar amounts if balance is known ─────────────────────
    if balance:
        for opp in top:
            opp.suggested_dollar = round(balance * opp.suggested_pct / 100, 0)

    return top


# ── sector scoring ────────────────────────────────────────────────────────

def _score_sector(ticker, name, tech, algo, news_score,
                  rl_target, rl_action, rl_confidence, vix):
    score     = 0.0
    rationale = []

    # Base: is this the RL pick?
    is_rl_pick = (ticker == rl_target and rl_action == "BUY")
    if is_rl_pick:
        score += 40 + rl_confidence * 0.3
        rationale.append(f"RL ensemble pick ({rl_confidence}% confidence)")
    elif rl_action == "BUY" and rl_confidence > 60:
        pass   # RL picked something else — this ticker starts neutral

    # Algo factor score
    algo_score = algo.get("score", 0)
    algo_elig  = algo.get("eligible", False)
    if algo_elig and algo_score > 0.3:
        score += 15
        rationale.append(f"algo-system eligible (score {algo_score:+.2f})")
    elif not algo_elig:
        score -= 8
        rationale.append("below 200d MA (algo filtered out)")

    # Technical: momentum + Sharpe + rel-strength
    mom   = tech.get("mom", 0)
    sharpe = tech.get("sharpe", 0)
    rel    = tech.get("rel", 0)
    rsi    = tech.get("rsi", 50)

    if mom > 0.03:
        score += 10; rationale.append(f"20d momentum {mom*100:+.1f}%")
    elif mom < -0.03:
        score -= 10; rationale.append(f"20d momentum weak ({mom*100:+.1f}%)")

    if sharpe > 1.0:
        score += 8
    elif sharpe < 0:
        score -= 8

    if rel > 0.01:
        score += 8; rationale.append(f"outperforming SPY +{rel*100:.1f}%")
    elif rel < -0.02:
        score -= 8

    # RSI sanity
    if 35 <= rsi <= 60:
        score += 5   # healthy entry zone
    elif rsi > 72:
        score -= 10; rationale.append(f"RSI {rsi} overbought — may be late")

    # News sentiment modifier
    if news_score is not None:
        if news_score >= 0.25:
            score += 8; rationale.append(f"news bullish ({news_score:+.2f})")
        elif news_score <= -0.25:
            score -= 10; rationale.append(f"news bearish ({news_score:+.2f})")

    # VIX penalty — elevated vol favors defensive
    if vix >= 30:
        score = max(0, score - 15)
    elif vix >= 25:
        score = max(0, score - 7)

    # Nothing positive → skip
    if score < 10 and not is_rl_pick:
        return None

    conviction, suggested_pct = _conviction_from_score(score, "sector", vix)
    if conviction == "AVOID":
        return None

    return Opportunity(
        ticker=ticker, name=name, asset_type="sector",
        conviction=conviction, score=round(score, 1),
        rationale=rationale, suggested_pct=suggested_pct,
        signal_summary={"rsi": rsi, "mom": mom, "sharpe": sharpe,
                        "rel": rel, "algo_score": algo_score,
                        "news": news_score},
    )


# ── crypto scoring ─────────────────────────────────────────────────────────

def _score_crypto(cs: dict, vix: float, existing_crypto_pct: float):
    ticker    = cs["ticker"]
    name      = cs["name"]
    atype     = cs["type"]
    signal    = cs.get("signal", "NEUTRAL")
    rsi       = cs.get("rsi", 50)
    mom_7d    = cs.get("mom_7d_pct", 0)
    chg_24h   = cs.get("change_24h_pct", 0)
    max_alloc = cs.get("max_alloc_pct", 3.0)

    score     = 30.0   # base — crypto always has speculative floor
    rationale = [f"{chg_24h:+.1f}% 24h  |  {mom_7d:+.1f}% 7d"]

    if signal == "STRONG_MOM":
        score += 25; rationale.append("strong momentum")
    elif signal == "OVERSOLD_BOUNCE":
        score += 18; rationale.append("oversold bounce setup")
    elif signal == "BULLISH":
        score += 10
    elif signal == "WEAK":
        score -= 15; rationale.append("weak / downtrend")
    elif signal == "OVERBOUGHT":
        score -= 8;  rationale.append(f"RSI {rsi} — extended")

    # Macro stress check — in crises, crypto dumps hardest
    if vix >= 30:
        score -= 20; rationale.append("high VIX — crypto risky in stressed markets")
    elif vix >= 25:
        score -= 8

    # Hard cap: never exceed remaining crypto budget
    remaining_crypto_budget = 10.0 - existing_crypto_pct
    cap = min(max_alloc, remaining_crypto_budget)

    if atype == "macro":
        conviction, suggested_pct = _conviction_from_score(score, "macro", vix)
    else:
        # crypto (BTC/ETH) — high-beta uncorrelated macro, 5% cap per coin
        conviction = "SPECULATIVE"
        suggested_pct = min(max_alloc, cap)
        if score >= 50:
            suggested_pct = min(max_alloc, cap)
        elif score >= 35:
            suggested_pct = min(max_alloc * 0.6, cap)
        else:
            conviction = "AVOID"
            suggested_pct = 0

    if conviction == "AVOID" or suggested_pct <= 0:
        return None

    return Opportunity(
        ticker=ticker, name=name, asset_type=atype,
        conviction=conviction, score=round(score, 1),
        rationale=rationale, suggested_pct=round(suggested_pct, 1),
        signal_summary={"rsi": rsi, "mom_7d": mom_7d,
                        "change_24h": chg_24h, "signal": signal},
    )


# ── conviction mapping ────────────────────────────────────────────────────

def _conviction_from_score(score, asset_type, vix):
    if asset_type == "macro":
        if score >= 55:
            return "MEDIUM", 12.0
        if score >= 40:
            return "LOW", 7.0
        return "AVOID", 0.0

    # sector
    if vix >= 30:
        max_pct = 15.0   # risk-off: trim all position sizes
    elif vix >= 25:
        max_pct = 20.0
    else:
        max_pct = 28.0

    if score >= 75:
        return "HIGH",   min(max_pct, 28.0)
    if score >= 55:
        return "MEDIUM", min(max_pct, 18.0)
    if score >= 35:
        return "LOW",    min(max_pct, 9.0)
    return "AVOID", 0.0


# ── formatting helpers (used by telegram_bot) ─────────────────────────────

CONVICTION_EMOJI = {
    "HIGH":        "🟢",
    "MEDIUM":      "🟡",
    "LOW":         "🔵",
    "SPECULATIVE": "🎲",
    "AVOID":       "🔴",
}

CONVICTION_LABEL = {
    "HIGH":        "HIGH CONVICTION",
    "MEDIUM":      "MEDIUM",
    "LOW":         "LOW",
    "SPECULATIVE": "SPECULATIVE",
}


def format_opportunity(opp: Opportunity, rank: int) -> str:
    """Single opportunity block for Telegram (HTML)."""
    emoji  = CONVICTION_EMOJI.get(opp.conviction, "⚪")
    label  = CONVICTION_LABEL.get(opp.conviction, opp.conviction)
    dollar = f"  ≈ <b>${opp.suggested_dollar:,.0f}</b>" if opp.suggested_dollar else ""
    pct    = f"{opp.suggested_pct:.0f}% of portfolio"

    lines = [
        f"{rank}. {emoji} <b>{opp.ticker}</b> — {opp.name}  [{label}]",
        f"   Sizing: {pct}{dollar}",
    ]
    for r in opp.rationale[:3]:
        lines.append(f"   • {r}")
    return "\n".join(lines)


if __name__ == "__main__":
    import json
    # Quick offline test
    fake_rl = {"target": "XLF", "action": "BUY", "confidence": 78,
               "vix": 18.0, "regime": "NORMAL"}
    ops = rank(fake_rl, balance=12000, max_results=5)
    for i, o in enumerate(ops, 1):
        print(format_opportunity(o, i))
        print()
