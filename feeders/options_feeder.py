"""
options_feeder.py — Put/Call ratio and options flow signals

The put/call ratio (PCR) is one of the oldest and most reliable contrarian
sentiment indicators. When PCR > 1.2, the market is buying protection (fear),
which is statistically associated with mean-reversion buying opportunities.
When PCR < 0.7, the market is very bullish on calls (complacency).

Data source: yfinance options chain (free, no key required).
Applied to SPY (market-wide) and the RL-targeted sector ETF.

PCR interpretation (contrarian):
  PCR > 1.2  → BEARISH (fear elevated, options-wise)  → slight confidence penalty
  PCR 0.7-1.2 → NEUTRAL
  PCR < 0.7  → BULLISH (complacency or genuine bullishness) → slight confidence boost

Use at sector level: sector ETFs with high PCR vs historical = sector-specific fear.
Best applied to liquid ETFs (SPY, QQQ, sector ETFs) — individual stocks less reliable.

Research: Codearmo (2024), LuxAlgo — PCR works best on major indices/liquid ETFs.
Combined with RSI: PCR > 1.2 + RSI < 40 = high-conviction oversold bounce setup.
"""

import datetime


def _get_pcr(ticker: str) -> float | None:
    """
    Compute put/call ratio (by open interest) for front-month options of ticker.
    Returns float (e.g., 1.15) or None on failure.
    """
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)
        expirations = t.options
        if not expirations:
            return None

        # Use front-month (nearest expiry at least 7 days out to avoid pin risk)
        today = datetime.date.today()
        target_exp = None
        for exp in expirations:
            try:
                exp_date = datetime.date.fromisoformat(exp)
                if (exp_date - today).days >= 7:
                    target_exp = exp
                    break
            except Exception:
                continue

        if not target_exp:
            target_exp = expirations[0]

        chain = t.option_chain(target_exp)
        calls = chain.calls
        puts  = chain.puts

        call_oi = calls["openInterest"].fillna(0).sum()
        put_oi  = puts["openInterest"].fillna(0).sum()

        if call_oi <= 0:
            return None

        return round(float(put_oi / call_oi), 3)
    except Exception as e:
        print(f"[options_feeder] PCR fetch failed for {ticker}: {e}")
        return None


def get_options_sentiment(tickers: list = None) -> dict:
    """
    Compute PCR for a list of tickers. Defaults to SPY + QQQ (market-wide).
    Returns {ticker: {"pcr": float, "signal": str, "interpretation": str}}

    Signal labels:
      FEARFUL    — PCR > 1.2  (elevated put buying = fear)
      NEUTRAL    — PCR 0.7-1.2
      COMPLACENT — PCR < 0.7  (call-heavy = optimism or complacency)
    """
    if tickers is None:
        tickers = ["SPY", "QQQ"]

    results = {}
    for ticker in tickers:
        pcr = _get_pcr(ticker)
        if pcr is None:
            continue

        if pcr > 1.2:
            signal = "FEARFUL"
            interp = f"PCR {pcr:.2f} — elevated put buying (fear/hedging)"
        elif pcr < 0.7:
            signal = "COMPLACENT"
            interp = f"PCR {pcr:.2f} — call-heavy positioning (optimism)"
        else:
            signal = "NEUTRAL"
            interp = f"PCR {pcr:.2f} — balanced options positioning"

        results[ticker] = {
            "pcr":            pcr,
            "signal":         signal,
            "interpretation": interp,
        }

    return results


def pcr_confidence_modifier(pcr_signals: dict, rl_action: str) -> tuple[float, list]:
    """
    Translate PCR signals into a confidence delta for the decision engine.
    Returns (delta, trace_notes).

    Only applied when RL action is BUY (short signals from PCR are pure context).
    Uses SPY as the market-wide signal, or average of available tickers.
    """
    if not pcr_signals or rl_action != "BUY":
        return 0.0, []

    spy = pcr_signals.get("SPY") or pcr_signals.get("QQQ")
    if not spy:
        return 0.0, []

    pcr = spy["pcr"]
    signal = spy["signal"]
    notes = []

    if signal == "FEARFUL":
        # High PCR = fear = contrarian buy signal (mean reversion)
        # But also means elevated risk → modest positive modifier
        delta = 4.0
        notes.append(f"Options PCR {pcr:.2f} — elevated put buying (contrarian +4% confidence)")
    elif signal == "COMPLACENT":
        # Very low PCR = complacency = slight caution (markets may be over-positioned)
        delta = -3.0
        notes.append(f"Options PCR {pcr:.2f} — call-heavy complacency (−3% confidence caution)")
    else:
        delta = 0.0

    return delta, notes


def format_options_block(pcr_signals: dict) -> str:
    """Format options sentiment for briefing display."""
    if not pcr_signals:
        return ""
    lines = ["⚙️ <b>Options Sentiment (PCR)</b>"]
    for ticker, data in pcr_signals.items():
        emoji = {"FEARFUL": "😰", "COMPLACENT": "😎", "NEUTRAL": "😐"}.get(data["signal"], "❓")
        lines.append(f"  {emoji} {ticker}: {data['interpretation']}")
    return "\n".join(lines)


if __name__ == "__main__":
    print("Testing options feeder (SPY + QQQ PCR)...")
    sigs = get_options_sentiment(["SPY", "QQQ"])
    for t, d in sigs.items():
        print(f"  {t}: PCR={d['pcr']}  signal={d['signal']}")
    delta, notes = pcr_confidence_modifier(sigs, "BUY")
    print(f"Confidence delta: {delta:+.1f}")
    for n in notes:
        print(f"  → {n}")
