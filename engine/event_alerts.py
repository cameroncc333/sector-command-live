"""
event_alerts.py — event-triggered alerts outside the 4 scheduled runs

Fires an EXTRA Telegram alert when something moves fast enough that waiting for the
next scheduled briefing would be too slow. Three triggers:

  1. VIX spike   — VIX up >20% vs yesterday's close (panic onset)
  2. Regime flip — calm/normal -> stressed (defensive posture warranted)
  3. Position hit — any held position down >5% intraday

Designed to run on a SEPARATE, more frequent cron (e.g. hourly during market hours)
as a lightweight check — it does NOT run the full RL decision, it just watches and
pings. Keeps state in a tiny JSON so it doesn't re-alert on the same event.

State-light, network-safe: if data is unavailable it simply does nothing.
"""

import os
import json
import datetime

STATE_PATH = os.environ.get("ALERT_STATE", os.path.join(os.path.dirname(__file__), "..", "data", "alert_state.json"))
VIX_SPIKE_PCT = 20.0     # % jump vs prior close
POSITION_DROP_PCT = -5.0 # intraday %


def _load_state():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump(state, f)


def _today():
    return datetime.date.today().isoformat()


def check_vix_spike():
    """Return alert string if VIX jumped >VIX_SPIKE_PCT vs prior close, else None."""
    try:
        import yfinance as yf
        raw = yf.download("^VIX", period="5d", progress=False, auto_adjust=True)
        close = raw["Close"]
        # single-ticker download can return a DataFrame; squeeze to a Series
        if hasattr(close, "columns"):
            close = close.iloc[:, 0]
        vix = close.dropna()
        if len(vix) < 2:
            return None, None
        today_v, prior_v = float(vix.iloc[-1]), float(vix.iloc[-2])
        pct = (today_v - prior_v) / prior_v * 100
        if pct >= VIX_SPIKE_PCT:
            return (f"⚡ VIX SPIKE: {prior_v:.1f} → {today_v:.1f} (+{pct:.0f}%). "
                    f"Volatility surging — system biases defensive (SPY/BIL) on next run."), round(today_v, 1)
        return None, round(today_v, 1)
    except Exception as e:
        print(f"[event_alerts] vix check skipped ({e})")
        return None, None


def check_regime_flip(current_regime, prior_regime):
    """Alert if regime flipped INTO stressed."""
    if prior_regime and prior_regime != "STRESSED" and current_regime == "STRESSED":
        return (f"⚠️ REGIME FLIP: {prior_regime} → STRESSED. "
                f"Drawdown penalties now amplified; defensive abstain actions active.")
    return None


def check_positions(holdings: dict):
    """
    holdings: {ticker: entry_price}. Alert on any position down > POSITION_DROP_PCT intraday.
    Returns a list of alert strings.
    """
    alerts = []
    if not holdings:
        return alerts
    try:
        import yfinance as yf
        tickers = list(holdings.keys())
        raw = yf.download(tickers, period="1d", progress=False, auto_adjust=True)
        close = raw["Close"] if "Close" in raw else raw
        for t, entry in holdings.items():
            try:
                if hasattr(close, "columns") and t in close.columns:
                    cur = float(close[t].dropna().iloc[-1])
                elif not hasattr(close, "columns"):
                    # single-ticker download returned a Series
                    cur = float(close.dropna().iloc[-1])
                else:
                    continue
                chg = (cur - entry) / entry * 100
                if chg <= POSITION_DROP_PCT:
                    alerts.append(f"🔻 {t} down {chg:.1f}% from entry (${entry:.2f}→${cur:.2f}). Review.")
            except Exception:
                continue
    except Exception as e:
        print(f"[event_alerts] position check skipped ({e})")
    return alerts


def check_sell_signals_alert(fired: set) -> tuple:
    """Check real holdings for exit signals and return any unfired alerts."""
    alerts = []
    new_fired = set()
    try:
        from engine.position_tracker import PositionTracker
        from engine.sell_signals import check_exit_signals
        holdings = PositionTracker().get_holdings()
        signals  = check_exit_signals(holdings)
        for sig in signals:
            key = f"sell_{sig['ticker']}_{sig['signal_type']}"
            if key not in fired:
                urgency_emoji = "🔴" if sig["urgency"] == "URGENT" else "🟡"
                alerts.append(f"{urgency_emoji} <b>{sig['ticker']}</b> EXIT SIGNAL [{sig['signal_type']}]\n"
                              f"   {sig['detail']}")
                new_fired.add(key)
    except Exception as e:
        print(f"[event_alerts] sell signal check skipped ({e})")
    return alerts, new_fired


def check_earnings_alert(fired: set) -> tuple:
    """Check for imminent earnings in held/tracked sectors."""
    alerts = []
    new_fired = set()
    try:
        from engine.position_tracker import PositionTracker
        from engine.earnings_calendar import upcoming_earnings_for_holdings
        sectors = ["XLK","XLF","XLE","XLV","XLY","XLP","XLI","XLB","XLRE","XLU","XLC"]
        held    = [h["ticker"] for h in PositionTracker().get_holdings() if h["ticker"] in sectors]
        earnings = upcoming_earnings_for_holdings(held, window_days=2)  # urgent: within 2 days
        for e in earnings:
            key = f"earn_{e['ticker']}_{e['earnings_date']}"
            if key not in fired:
                days = e["days_away"]
                day_str = "TODAY" if days == 0 else "TOMORROW" if days == 1 else f"in {days} days"
                alerts.append(f"📅 <b>{e['ticker']}</b> ({e['sector_etf']}) earnings {day_str}. "
                              f"Consider sizing down before report.")
                new_fired.add(key)
    except Exception as e:
        print(f"[event_alerts] earnings alert check skipped ({e})")
    return alerts, new_fired


def run(current_regime=None, holdings=None):
    """
    Main entry for the lightweight cron. Sends any triggered alerts via Telegram,
    dedups against today's state so it won't spam the same event.
    """
    from interface.telegram_bot import TelegramBot
    state = _load_state()
    today = _today()
    if state.get("date") != today:
        state = {"date": today, "fired": []}

    fired = set(state.get("fired", []))
    messages = []

    vix_msg, vix_val = check_vix_spike()
    if vix_msg and "vix_spike" not in fired:
        messages.append(vix_msg); fired.add("vix_spike")

    if current_regime:
        flip = check_regime_flip(current_regime, state.get("last_regime"))
        if flip and "regime_flip" not in fired:
            messages.append(flip); fired.add("regime_flip")
        state["last_regime"] = current_regime

    for a in check_positions(holdings or {}):
        key = "pos_" + a.split()[1]
        if key not in fired:
            messages.append(a); fired.add(key)

    # Sell signal check (real holdings)
    sell_alerts, sell_fired = check_sell_signals_alert(fired)
    messages.extend(sell_alerts)
    fired.update(sell_fired)

    # Earnings proximity alert
    earn_alerts, earn_fired = check_earnings_alert(fired)
    messages.extend(earn_alerts)
    fired.update(earn_fired)

    if messages:
        bot = TelegramBot()
        bot.send_message("🚨 <b>SECTOR COMMAND — Event Alert</b>\n\n" + "\n\n".join(messages))

    state["fired"] = list(fired)
    _save_state(state)
    return messages


if __name__ == "__main__":
    # offline-safe demo
    print("VIX check:", check_vix_spike())
    print("Regime flip NORMAL->STRESSED:", check_regime_flip("STRESSED", "NORMAL"))
    print("Regime flip none:", check_regime_flip("NORMAL", "NORMAL"))
