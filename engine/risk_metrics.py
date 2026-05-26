"""
risk_metrics.py — quantitative risk analytics

Computes:
  VaR(95%)       — worst expected 1-day loss 95% of the time
  CVaR(95%)      — mean loss on the worst 5% of days (tail risk / Expected Shortfall)
  Kelly f*       — optimal position fraction; half-Kelly applied for safety
  Sharpe decomp  — annualized return and vol reported separately, not just ratio
  Yield curve    — 10yr minus 2yr spread; inversion = recession signal
  Macro snapshot — DXY dollar index, 10yr rate, yield spread

These are the metrics actual portfolio managers use. VaR/CVaR in particular make
the whitepaper significantly stronger — a Sharpe ratio without tail risk context
is considered incomplete in professional settings.
"""

import numpy as np
import datetime

# Treasury yield tickers (yfinance)
_T10 = "^TNX"   # 10-year yield (%)
_T2  = "^IRX"   # 13-week T-bill (closest free proxy for 2yr on yfinance)
_DXY = "DX-Y.NYB"  # US Dollar Index


def _download(tickers, period="1y"):
    try:
        import yfinance as yf
        data = yf.download(tickers, period=period, progress=False, auto_adjust=True)
        if data is None or len(data) == 0:
            return None
        close = data["Close"] if hasattr(data.columns, "get_level_values") and "Close" in data.columns.get_level_values(0) else data
        return close
    except Exception as e:
        print(f"[risk_metrics] download failed: {e}")
        return None


# ── VaR / CVaR ────────────────────────────────────────────────────────────────

def portfolio_var(holdings: list, confidence: float = 0.95, period: str = "1y") -> dict:
    """
    Compute VaR and CVaR for each held position.
    holdings: list of dicts from position_tracker.get_holdings()
    Returns {ticker: {var_pct, cvar_pct, vol_annual, var_dollar, cvar_dollar}}
    """
    if not holdings:
        return {}

    tickers = [h["ticker"] for h in holdings]
    closes  = _download(tickers, period=period)
    if closes is None:
        return {}

    results = {}
    for h in holdings:
        t = h["ticker"]
        col = t if hasattr(closes, "columns") and t in closes.columns else None
        if col is None and not hasattr(closes, "columns"):
            col = "_single"
            series = closes.dropna()
        elif col:
            series = closes[col].dropna()
        else:
            continue

        if len(series) < 20:
            continue

        rets = series.pct_change().dropna().values
        rets_sorted = np.sort(rets)

        cutoff_idx   = int(len(rets_sorted) * (1 - confidence))
        var_pct      = float(-np.percentile(rets, (1 - confidence) * 100))
        cvar_pct     = float(-rets_sorted[:max(cutoff_idx, 1)].mean())
        vol_annual   = float(rets.std() * np.sqrt(252))

        position_val = h.get("current_value") or h.get("cost_basis") or h.get("dollar_value") or 0
        results[t] = {
            "var_pct":    round(var_pct * 100, 2),       # as % of position
            "cvar_pct":   round(cvar_pct * 100, 2),
            "vol_annual":  round(vol_annual * 100, 1),
            "var_dollar":  round(var_pct * position_val, 2),
            "cvar_dollar": round(cvar_pct * position_val, 2),
            "position_val": round(position_val, 2),
        }
    return results


def portfolio_var_summary(var_data: dict) -> dict:
    """Aggregate across all positions into a single portfolio-level view."""
    if not var_data:
        return {}
    total_var   = sum(v["var_dollar"]  for v in var_data.values())
    total_cvar  = sum(v["cvar_dollar"] for v in var_data.values())
    total_val   = sum(v["position_val"] for v in var_data.values())
    return {
        "total_var_dollar":  round(total_var, 2),
        "total_cvar_dollar": round(total_cvar, 2),
        "total_val":         round(total_val, 2),
        "portfolio_var_pct": round(total_var / total_val * 100, 2) if total_val else 0,
        "portfolio_cvar_pct":round(total_cvar / total_val * 100, 2) if total_val else 0,
    }


# ── Kelly Criterion ───────────────────────────────────────────────────────────

def kelly_fraction(win_prob: float, avg_win_pct: float, avg_loss_pct: float,
                   half_kelly: bool = True) -> float:
    """
    Full Kelly: f* = (p*b - q) / b   where b = avg_win / avg_loss, q = 1 - p
    Half-Kelly applied by default (standard conservative practice).
    Returns fraction of portfolio to risk (0.0 – 0.35 capped).
    """
    if avg_loss_pct <= 0:
        return 0.0
    b = avg_win_pct / avg_loss_pct
    q = 1 - win_prob
    f = (win_prob * b - q) / b
    if half_kelly:
        f *= 0.5
    return round(max(0.0, min(f, 0.35)), 3)


def kelly_from_signals(rl_confidence: float, momentum: float, sharpe: float) -> float:
    """
    Derive Kelly fraction from live signals when historical win/loss data is sparse.
    Uses RL confidence as a proxy for win probability and momentum/Sharpe
    to estimate expected payoff ratio. Conservative by design.

    rl_confidence: 0–100
    momentum:      20-day price return (decimal, e.g. 0.03 = 3%)
    sharpe:        6-month rolling Sharpe ratio
    """
    p     = max(0.45, min(rl_confidence / 100, 0.80))  # floor at 45%, cap at 80%
    # Expected payoff: base 2:1 scaled by momentum and Sharpe
    payoff = 1.0 + abs(momentum) * 5 + max(0, sharpe) * 0.2
    payoff = max(1.1, min(payoff, 4.0))   # clamp to reasonable range
    return kelly_fraction(p, payoff, 1.0, half_kelly=True)


def kelly_for_opportunity(opp: dict) -> float:
    """
    Compute Kelly fraction for a ranked opportunity dict.
    Falls back gracefully if signals are missing.
    """
    sig     = opp.get("signal_summary", {})
    conf    = opp.get("score", 50) / 100     # use composite score as win prob proxy
    mom     = sig.get("mom") or sig.get("mom_7d", 0) or 0
    sharpe  = sig.get("sharpe", 0) or 0
    return kelly_from_signals(conf * 100, mom, sharpe)


# ── Sharpe Decomposition ──────────────────────────────────────────────────────

def sharpe_decomp(ticker: str, period: str = "1y") -> dict:
    """
    Return annualized return, annualized vol, and Sharpe separately.
    Sharpe alone hides whether you're winning because of high return or low risk —
    decomposing it makes the whitepaper more defensible.
    """
    closes = _download(ticker, period=period)
    if closes is None:
        return {}
    s = closes.dropna() if not hasattr(closes, "columns") else closes[ticker].dropna() if ticker in closes.columns else None
    if s is None or len(s) < 20:
        return {}
    rets         = s.pct_change().dropna()
    ann_return   = float(rets.mean() * 252)
    ann_vol      = float(rets.std() * np.sqrt(252))
    sharpe       = float(ann_return / ann_vol) if ann_vol > 0 else 0.0
    max_dd       = float(_max_drawdown(s))
    return {
        "ticker":       ticker,
        "ann_return":   round(ann_return * 100, 2),   # %
        "ann_vol":      round(ann_vol * 100, 2),       # %
        "sharpe":       round(sharpe, 2),
        "max_drawdown": round(max_dd * 100, 2),        # %
        "calmar":       round(ann_return / abs(max_dd), 2) if max_dd != 0 else 0,
    }


def _max_drawdown(prices):
    roll_max = prices.expanding().max()
    dd = (prices - roll_max) / roll_max
    return float(dd.min())


# ── Macro Indicators ──────────────────────────────────────────────────────────

def yield_curve() -> dict:
    """
    Yield curve spread: 10yr minus 13-week T-bill (free yfinance proxy for 2yr).
    Negative spread = inversion = leading recession indicator.
    """
    try:
        import yfinance as yf
        t10_raw = yf.download(_T10, period="5d", progress=False, auto_adjust=True)
        t2_raw  = yf.download(_T2,  period="5d", progress=False, auto_adjust=True)

        def _last(raw):
            c = raw["Close"] if hasattr(raw.columns, "get_level_values") and "Close" in raw.columns.get_level_values(0) else raw
            if hasattr(c, "columns"):
                c = c.iloc[:, 0]
            return float(c.dropna().iloc[-1])

        t10 = _last(t10_raw)
        t2  = _last(t2_raw)
        spread = round(t10 - t2, 3)
        return {
            "ten_yr":   round(t10, 3),
            "two_yr":   round(t2, 3),
            "spread":   spread,
            "inverted": spread < 0,
            "signal":   ("INVERTED — recession risk elevated" if spread < 0
                         else "NORMAL" if spread > 0.5
                         else "FLAT — watch closely"),
        }
    except Exception as e:
        print(f"[risk_metrics] yield curve unavailable: {e}")
        return {}


def macro_snapshot() -> dict:
    """
    Quick macro dashboard: yield curve + dollar index.
    Used by the Telegram briefing and dashboard.
    """
    yc = yield_curve()
    dollar = {}
    try:
        import yfinance as yf
        raw = yf.download(_DXY, period="5d", progress=False, auto_adjust=True)
        c   = raw["Close"] if hasattr(raw.columns, "get_level_values") and "Close" in raw.columns.get_level_values(0) else raw
        if hasattr(c, "columns"):
            c = c.iloc[:, 0]
        vals = c.dropna()
        if len(vals) >= 2:
            dxy_now  = float(vals.iloc[-1])
            dxy_prev = float(vals.iloc[-2])
            dollar = {
                "dxy": round(dxy_now, 2),
                "dxy_change": round((dxy_now - dxy_prev) / dxy_prev * 100, 2),
                "signal": "STRONG USD" if dxy_now > 104 else "WEAK USD" if dxy_now < 100 else "NEUTRAL",
            }
    except Exception as e:
        print(f"[risk_metrics] DXY unavailable: {e}")

    return {"yield_curve": yc, "dollar": dollar}


# ── Sector rotation (4-week relative strength) ────────────────────────────────

SECTORS = ["XLK","XLF","XLE","XLV","XLY","XLP","XLI","XLB","XLRE","XLU","XLC"]

def sector_rotation_matrix(weeks: int = 4) -> dict:
    """
    Returns relative strength (vs SPY) for each sector over the last N weeks.
    Result: {ticker: [week1_rel, week2_rel, week3_rel, week4_rel]}
    week1 = most recent, week4 = oldest. Values are % outperformance vs SPY.
    Used by the dashboard heatmap.
    """
    try:
        import yfinance as yf
        tickers = SECTORS + ["SPY"]
        period  = f"{weeks * 7 + 10}d"
        raw     = yf.download(tickers, period=period, progress=False, auto_adjust=True)
        close   = raw["Close"] if hasattr(raw.columns, "get_level_values") and "Close" in raw.columns.get_level_values(0) else raw
        if close is None or len(close) < 5:
            return {}

        spy = close["SPY"].dropna()
        result = {}
        for t in SECTORS:
            if t not in close.columns:
                continue
            s   = close[t].dropna()
            row = []
            for w in range(weeks):
                # weekly slice: last 5 trading days for week 0, prior 5 for week 1, etc.
                end   = -(w * 5) if w > 0 else None
                start = -(w * 5 + 5)
                try:
                    if end:
                        t_ret   = float(s.iloc[start:end].iloc[-1]  / s.iloc[start:end].iloc[0]  - 1)
                        spy_ret = float(spy.iloc[start:end].iloc[-1] / spy.iloc[start:end].iloc[0] - 1)
                    else:
                        t_ret   = float(s.iloc[start:].iloc[-1]  / s.iloc[start:].iloc[0]  - 1)
                        spy_ret = float(spy.iloc[start:].iloc[-1] / spy.iloc[start:].iloc[0] - 1)
                    row.append(round((t_ret - spy_ret) * 100, 2))
                except Exception:
                    row.append(0.0)
            result[t] = row
        return result
    except Exception as e:
        print(f"[risk_metrics] rotation matrix unavailable: {e}")
        return {}


# ── Formatting helpers ────────────────────────────────────────────────────────

def format_risk_block(var_data: dict, macro: dict) -> str:
    """Compact risk summary for Telegram (shown in PERF response)."""
    lines = []

    yc = macro.get("yield_curve", {})
    if yc:
        inv = "⚠️ INVERTED" if yc.get("inverted") else "Normal"
        lines.append(f"📐 Yield curve: {yc.get('ten_yr','?')}% (10yr) − {yc.get('two_yr','?')}% (2yr) = "
                     f"{yc.get('spread','?')}  [{inv}]")

    dxy = macro.get("dollar", {})
    if dxy:
        chg = dxy.get("dxy_change", 0)
        lines.append(f"💵 DXY: {dxy.get('dxy','?')}  ({chg:+.2f}%)  [{dxy.get('signal','')}]")

    if var_data:
        lines.append("")
        lines.append("📊 <b>Position Risk (95% VaR)</b>")
        for t, v in var_data.items():
            lines.append(f"  {t}: VaR ${v['var_dollar']:,.0f} ({v['var_pct']:.1f}%) · "
                         f"CVaR ${v['cvar_dollar']:,.0f} ({v['cvar_pct']:.1f}%) · "
                         f"Vol {v['vol_annual']:.1f}%/yr")
        summary = portfolio_var_summary(var_data)
        if summary:
            lines.append(f"  Total: VaR ${summary['total_var_dollar']:,.0f} ({summary['portfolio_var_pct']:.1f}%) · "
                         f"CVaR ${summary['total_cvar_dollar']:,.0f} ({summary['portfolio_cvar_pct']:.1f}%)")

    return "\n".join(lines)


if __name__ == "__main__":
    import json
    print("Macro snapshot:")
    m = macro_snapshot()
    print(json.dumps(m, indent=2))
    print("\nKelly test (conf=78, mom=0.03, sharpe=1.2):")
    print(kelly_from_signals(78, 0.03, 1.2))
    print("\nRotation matrix (2 weeks):")
    rot = sector_rotation_matrix(2)
    for t, v in list(rot.items())[:3]:
        print(f"  {t}: {v}")
