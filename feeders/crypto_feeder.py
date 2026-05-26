"""
crypto_feeder.py — live signals for BTC + ETH (institutional crypto) + gold/macro hedges

Crypto universe is intentionally narrow: Bitcoin and Ethereum only.
They are treated as high-beta uncorrelated macro assets — consistent with
modern portfolio theory, appropriate for a quantitative research context.

All tickers pulled via yfinance. No API keys required.
Degrades gracefully if network is unavailable.

Position-size caps (enforced by multi_asset_ranker):
  Crypto (BTC, ETH) — max 5% each, 10% combined
  Macro hedges      — GLD, TLT, QQQ up to 15% each
"""

import numpy as np

MAJOR_CRYPTO = ["BTC-USD", "ETH-USD"]
MACRO_HEDGES = ["GLD", "TLT", "QQQ"]

ASSET_NAMES = {
    "BTC-USD": "Bitcoin",
    "ETH-USD": "Ethereum",
    "GLD":     "Gold ETF",
    "TLT":     "20Y Treasuries",
    "QQQ":     "Nasdaq 100",
}

# Max single-asset allocation as % of portfolio
MAX_ALLOC = {
    "crypto": 5.0,   # 5% per coin, 10% combined hard cap
    "macro":  15.0,  # gold/bonds/QQQ
}


def _safe_download(tickers, period="60d"):
    """Wraps yfinance, returns Close DataFrame or None."""
    try:
        import yfinance as yf
        data = yf.download(tickers, period=period, progress=False, auto_adjust=True)
        if data is None or len(data) == 0:
            return None
        has_levels = hasattr(data.columns, "get_level_values")
        close = data["Close"] if (has_levels and "Close" in data.columns.get_level_values(0)) else data
        # Single-ticker download returns Series
        if hasattr(close, "to_frame"):
            name = tickers[0] if isinstance(tickers, list) else tickers
            return close.to_frame(name=name)
        return close
    except Exception as e:
        print(f"[crypto_feeder] download failed ({e})")
        return None


def _rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _signal(rsi, mom_24h, mom_7d):
    if rsi < 32 and mom_7d > 0:
        return "OVERSOLD_BOUNCE"
    if rsi > 72:
        return "OVERBOUGHT"
    if mom_24h > 0.04 and mom_7d > 0.06:
        return "STRONG_MOM"
    if mom_24h < -0.04 and mom_7d < -0.06:
        return "WEAK"
    if mom_7d > 0.02:
        return "BULLISH"
    if mom_7d < -0.02:
        return "BEARISH"
    return "NEUTRAL"


def _fmt_price(p):
    if p < 0.0001:
        return round(p, 8)
    if p < 0.01:
        return round(p, 6)
    if p < 1:
        return round(p, 4)
    if p < 10:
        return round(p, 3)
    return round(p, 2)


def get_crypto_signals():
    """
    Returns dict keyed by ticker. Each value:
      {ticker, name, type, price, change_24h_pct, mom_7d_pct, rsi,
       vol_7d_pct, signal, max_alloc_pct}
    Returns {} if network unavailable.
    """
    all_tickers = MAJOR_CRYPTO + MACRO_HEDGES
    closes = _safe_download(all_tickers, period="60d")
    if closes is None:
        return {}

    out = {}
    for t in all_tickers:
        if t not in closes.columns:
            continue
        s = closes[t].dropna()
        if len(s) < 5:
            continue

        price   = float(s.iloc[-1])
        chg_24h = float(s.iloc[-1] / s.iloc[-2] - 1) if len(s) >= 2 else 0.0
        mom_7d  = float(s.iloc[-1] / s.iloc[-8] - 1) if len(s) >= 8 else 0.0
        vol_7d  = float(s.pct_change().tail(7).std()) if len(s) >= 7 else 0.0
        rsi_val = float(_rsi(s).iloc[-1]) if len(s) >= 16 else 50.0

        asset_type = "crypto" if t in MAJOR_CRYPTO else "macro"

        out[t] = {
            "ticker":          t,
            "name":            ASSET_NAMES.get(t, t),
            "type":            asset_type,
            "price":           _fmt_price(price),
            "change_24h_pct":  round(chg_24h * 100, 2),
            "mom_7d_pct":      round(mom_7d * 100, 2),
            "rsi":             round(rsi_val, 1),
            "vol_7d_pct":      round(vol_7d * 100, 2),
            "signal":          _signal(rsi_val, chg_24h, mom_7d),
            "max_alloc_pct":   MAX_ALLOC[asset_type],
        }
    return out


def top_crypto(signals=None, n=3):
    """
    Returns up to n major crypto tickers ranked by momentum + RSI composite.
    Meme coins are excluded from rankings — they appear as a separate category.
    """
    sigs = signals or get_crypto_signals()
    majors = {t: v for t, v in sigs.items() if v["type"] == "crypto"}
    if not majors:
        return []
    # simple score: normalize 7d mom + RSI distance from 50
    scored = sorted(
        majors.items(),
        key=lambda kv: kv[1]["mom_7d_pct"] + (50 - abs(kv[1]["rsi"] - 50)) * 0.3,
        reverse=True,
    )
    return [t for t, _ in scored[:n]]


if __name__ == "__main__":
    import json
    sigs = get_crypto_signals()
    print(json.dumps(sigs, indent=2))
    print("\nTop crypto:", top_crypto(sigs))
