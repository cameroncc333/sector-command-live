"""
sell_signals.py — exit signal detector for real holdings

Scans every position in position_tracker and fires alerts when:
  1. TRAILING STOP   — price has fallen 5% from the highest point since you bought
  2. RSI OVERBOUGHT  — RSI(14) > 72: statistically extended, mean-reversion risk
  3. MOMENTUM FLIP   — 20-day momentum turned negative after a positive entry
  4. TIME + LOSS     — held > 45 days while still at a loss (opportunity cost flag)

Urgency levels:
  URGENT  — act now (trailing stop triggered, or RSI > 78)
  WATCH   — review at next briefing (momentum flip, time+loss, RSI 72-78)

These signals never force a sell — they alert you so YOU decide.
The governance layer only blocks buys; exits are always your call.
"""

import datetime
import numpy as np


# ── Thresholds ────────────────────────────────────────────────────────────────
TRAILING_STOP_PCT  = 5.0    # % below the highest price since entry
RSI_URGENT         = 78.0   # RSI this high: urgent review
RSI_WATCH          = 72.0   # RSI this high: watch
HOLD_DAYS_FLAG     = 45     # flag if held this many days at a loss
MOMENTUM_WINDOW    = 20     # days for momentum flip check


def _download_prices(tickers, period="6mo"):
    try:
        import yfinance as yf
        raw   = yf.download(tickers, period=period, progress=False, auto_adjust=True)
        close = raw["Close"] if hasattr(raw.columns, "get_level_values") and "Close" in raw.columns.get_level_values(0) else raw
        return close
    except Exception as e:
        print(f"[sell_signals] price download failed: {e}")
        return None


def _rsi(series, period=14):
    delta = series.diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def check_exit_signals(holdings: list) -> list:
    """
    Main entry. holdings = position_tracker.get_holdings().
    Returns list of signal dicts:
      {ticker, signal_type, urgency, detail, current_price, entry_price}
    """
    if not holdings:
        return []

    tickers = [h["ticker"] for h in holdings]
    closes  = _download_prices(tickers, period="6mo")
    if closes is None:
        return []

    signals = []
    today   = datetime.date.today()

    for h in holdings:
        t          = h["ticker"]
        entry_price = h.get("avg_cost")
        date_bought = h.get("date_bought")

        # Get the price series for this ticker
        if hasattr(closes, "columns") and t in closes.columns:
            series = closes[t].dropna()
        elif not hasattr(closes, "columns"):
            series = closes.dropna()
        else:
            continue

        if len(series) < 5:
            continue

        current_price = float(series.iloc[-1])

        # ── 1. Trailing stop ────────────────────────────────────────────────
        if entry_price and entry_price > 0:
            high_since_entry = float(series.iloc[-min(len(series), 126):].max())
            drop_from_high   = (current_price - high_since_entry) / high_since_entry * 100
            if drop_from_high <= -TRAILING_STOP_PCT:
                signals.append({
                    "ticker":        t,
                    "signal_type":   "TRAILING_STOP",
                    "urgency":       "URGENT",
                    "detail":        f"Down {abs(drop_from_high):.1f}% from recent high of ${high_since_entry:.2f}. Entry: ${entry_price:.2f}",
                    "current_price": round(current_price, 2),
                    "entry_price":   round(entry_price, 2),
                })
                continue   # don't stack other signals if stop already triggered

        # ── 2. RSI overbought ───────────────────────────────────────────────
        if len(series) >= 16:
            rsi_val = float(_rsi(series).iloc[-1])
            if rsi_val >= RSI_URGENT:
                signals.append({
                    "ticker":        t,
                    "signal_type":   "RSI_OVERBOUGHT",
                    "urgency":       "URGENT",
                    "detail":        f"RSI {rsi_val:.1f} — extremely extended. Mean reversion likely.",
                    "current_price": round(current_price, 2),
                    "entry_price":   round(entry_price, 2) if entry_price else None,
                })
            elif rsi_val >= RSI_WATCH:
                signals.append({
                    "ticker":        t,
                    "signal_type":   "RSI_OVERBOUGHT",
                    "urgency":       "WATCH",
                    "detail":        f"RSI {rsi_val:.1f} — overbought zone. Monitor.",
                    "current_price": round(current_price, 2),
                    "entry_price":   round(entry_price, 2) if entry_price else None,
                })

        # ── 3. Momentum flip ────────────────────────────────────────────────
        if len(series) >= MOMENTUM_WINDOW + 5:
            mom_now  = float(series.iloc[-1]  / series.iloc[-MOMENTUM_WINDOW]  - 1)
            mom_prev = float(series.iloc[-3]  / series.iloc[-(MOMENTUM_WINDOW+3)] - 1)
            if mom_now < 0 and mom_prev >= 0:   # just turned negative
                signals.append({
                    "ticker":        t,
                    "signal_type":   "MOMENTUM_FLIP",
                    "urgency":       "WATCH",
                    "detail":        f"20d momentum flipped negative ({mom_now*100:+.1f}%). Trend reversing.",
                    "current_price": round(current_price, 2),
                    "entry_price":   round(entry_price, 2) if entry_price else None,
                })

        # ── 4. Time + loss flag ─────────────────────────────────────────────
        if entry_price and date_bought:
            try:
                bought_date = datetime.date.fromisoformat(date_bought)
                days_held   = (today - bought_date).days
                pnl_pct     = (current_price - entry_price) / entry_price * 100
                if days_held >= HOLD_DAYS_FLAG and pnl_pct < 0:
                    signals.append({
                        "ticker":        t,
                        "signal_type":   "STALE_LOSS",
                        "urgency":       "WATCH",
                        "detail":        f"Held {days_held}d at {pnl_pct:+.1f}%. Consider cutting for a better opportunity.",
                        "current_price": round(current_price, 2),
                        "entry_price":   round(entry_price, 2),
                    })
            except Exception:
                pass

    return signals


def format_sell_alerts(signals: list) -> str:
    """Format sell signals for the Telegram briefing or event alert."""
    if not signals:
        return ""

    urgent = [s for s in signals if s["urgency"] == "URGENT"]
    watch  = [s for s in signals if s["urgency"] == "WATCH"]

    lines = ["🚨 <b>Exit Signals — Your Holdings</b>"]

    if urgent:
        for s in urgent:
            emoji = "🔴"
            lines.append(f"  {emoji} <b>{s['ticker']}</b> [{s['signal_type']}] — URGENT")
            lines.append(f"     {s['detail']}")
            lines.append(f"     Current: ${s['current_price']:,.2f}" +
                         (f" | Entry: ${s['entry_price']:,.2f}" if s.get("entry_price") else ""))

    if watch:
        for s in watch:
            lines.append(f"  🟡 <b>{s['ticker']}</b> [{s['signal_type']}] — Watch")
            lines.append(f"     {s['detail']}")

    lines.append("\nReply <code>SOLD TICKER</code> after selling.")
    return "\n".join(lines)


if __name__ == "__main__":
    # Offline test with dummy holding
    dummy = [{
        "ticker": "XLF", "avg_cost": 48.00, "dollar_value": 1000,
        "date_bought": "2026-01-15", "current_value": 950,
    }]
    sigs = check_exit_signals(dummy)
    print("Signals:", sigs)
    print(format_sell_alerts(sigs))
