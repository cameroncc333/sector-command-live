"""
alpaca_executor.py — paper trade execution via Alpaca Markets API

When the user replies BUY/SELL/SKIP to a Telegram briefing, this module
executes the trade on Alpaca's free paper-trading environment. Alpaca tracks
the full portfolio history, so P&L is REAL (as in broker-logged), not estimated.

Setup (5 minutes):
  1. Sign up at alpaca.markets → choose "Paper Trading"
  2. Dashboard → API Keys → create a Paper Trading key pair
  3. Add as GitHub Secrets: ALPACA_API_KEY, ALPACA_SECRET_KEY
  4. Set ALPACA_PAPER=true in your env (already assumed here)

Position sizing:
  - Account starts at $100,000 paper cash (Alpaca default)
  - BUY: allocate MAX_POSITION_PCT of account to the sector ETF (market order)
  - SELL: close the entire position
  - If currently in SPY/BIL (abstain): treated as a normal BUY
"""

import os
import json
import requests

PAPER_BASE = "https://paper-api.alpaca.markets"
MAX_POSITION_PCT = 0.30   # max 30% per position (matches governance rule)

_KEY  = os.environ.get("ALPACA_API_KEY",    "")
_SEC  = os.environ.get("ALPACA_SECRET_KEY", "")
_HEADERS = {"APCA-API-KEY-ID": _KEY, "APCA-API-SECRET-KEY": _SEC}


def _get(path, **params):
    if not _KEY or not _SEC:
        return None
    try:
        r = requests.get(f"{PAPER_BASE}{path}", headers=_HEADERS, params=params, timeout=15)
        return r.json() if r.ok else None
    except Exception as e:
        print(f"[alpaca] GET {path} failed: {e}")
        return None


def _post(path, body):
    if not _KEY or not _SEC:
        print("[alpaca] No API keys — skipping order (set ALPACA_API_KEY + ALPACA_SECRET_KEY)")
        return None
    try:
        r = requests.post(f"{PAPER_BASE}{path}", headers=_HEADERS, json=body, timeout=15)
        return r.json() if r.ok else {"error": r.text}
    except Exception as e:
        print(f"[alpaca] POST {path} failed: {e}")
        return None


def _delete(path):
    if not _KEY or not _SEC:
        return None
    try:
        r = requests.delete(f"{PAPER_BASE}{path}", headers=_HEADERS, timeout=15)
        return r.status_code
    except Exception as e:
        print(f"[alpaca] DELETE {path} failed: {e}")
        return None


def get_account():
    """Return account dict with equity, buying_power, etc."""
    return _get("/v2/account") or {}


def get_positions():
    """Return list of open positions."""
    return _get("/v2/positions") or []


def get_portfolio_history(period="1M", timeframe="1D"):
    """Return portfolio history for the equity curve chart."""
    return _get("/v2/account/portfolio/history", period=period, timeframe=timeframe) or {}


def close_all_positions():
    """Close all open positions (used before taking a new sector position)."""
    positions = get_positions()
    closed = []
    for p in positions:
        sym = p.get("symbol")
        result = _delete(f"/v2/positions/{sym}")
        closed.append({"symbol": sym, "status": result})
        print(f"  [alpaca] Closed {sym}")
    return closed


def buy_sector(ticker: str, confidence: int = 50) -> dict:
    """
    Buy a sector ETF.
    Allocates MAX_POSITION_PCT of account equity, scaled by confidence:
      - confidence >= 75 → 30% (full position)
      - confidence 50-74 → 20% (medium)
      - confidence < 50  → 10% (small)
    Closes any existing non-matching positions first.
    """
    account = get_account()
    equity = float(account.get("equity", 0) or 0)
    if equity == 0:
        print("[alpaca] Could not get account equity — order skipped")
        return {"error": "no_account"}

    # Scale allocation by conviction
    if confidence >= 75:
        alloc_pct = MAX_POSITION_PCT
    elif confidence >= 50:
        alloc_pct = 0.20
    else:
        alloc_pct = 0.10

    notional = round(equity * alloc_pct, 2)

    # Close existing positions before entering new one (simple rotation)
    close_all_positions()

    body = {
        "symbol": ticker,
        "notional": str(notional),
        "side": "buy",
        "type": "market",
        "time_in_force": "day",
    }
    result = _post("/v2/orders", body)
    print(f"  [alpaca] BUY {ticker} ${notional:.0f} ({alloc_pct:.0%} of ${equity:.0f})")
    return result or {}


def sell_sector(ticker: str) -> dict:
    """Close the position in ticker (if held)."""
    result = _delete(f"/v2/positions/{ticker}")
    print(f"  [alpaca] SELL/CLOSE {ticker}: {result}")
    return {"status": result}


def execute_command(command: str, ticker: str, confidence: int = 50) -> dict:
    """
    Main entry point called by webhook.py after a human reply.
    Returns a result dict logged to the journal.
    """
    if not _KEY or not _SEC:
        return {"executed": False, "reason": "no_alpaca_keys"}

    cmd = command.upper()
    if cmd == "BUY" and ticker:
        result = buy_sector(ticker, confidence)
        return {"executed": True, "action": "BUY", "ticker": ticker,
                "notional": result.get("notional"), "order_id": result.get("id")}
    elif cmd in ("SELL", "CLOSE") and ticker:
        result = sell_sector(ticker)
        return {"executed": True, "action": "SELL", "ticker": ticker}
    elif cmd == "SKIP":
        return {"executed": False, "action": "SKIP"}
    else:
        return {"executed": False, "reason": f"unhandled: {cmd} {ticker}"}


def portfolio_summary() -> dict:
    """
    Return a summary dict for the dashboard and briefing.
    Includes account equity, open positions, daily P&L.
    """
    account = get_account()
    positions = get_positions()
    if not account:
        return {}

    equity = float(account.get("equity", 0) or 0)
    last_equity = float(account.get("last_equity", equity) or equity)
    daily_pnl = equity - last_equity
    daily_pnl_pct = (daily_pnl / last_equity * 100) if last_equity else 0

    pos_list = []
    for p in positions:
        pos_list.append({
            "symbol": p.get("symbol"),
            "qty": p.get("qty"),
            "current_price": p.get("current_price"),
            "unrealized_pl": p.get("unrealized_pl"),
            "unrealized_plpc": p.get("unrealized_plpc"),
        })

    return {
        "equity": round(equity, 2),
        "cash": round(float(account.get("cash", 0) or 0), 2),
        "daily_pnl": round(daily_pnl, 2),
        "daily_pnl_pct": round(daily_pnl_pct, 2),
        "positions": pos_list,
        "account_status": account.get("status"),
    }


if __name__ == "__main__":
    summary = portfolio_summary()
    print(json.dumps(summary, indent=2))
