"""
repo_signals.py — the multi-repo signal aggregator (THE missing piece)

This is what makes Sector Command genuinely "all repos feeding one decision"
instead of just the RL agent acting alone. It computes, live, the same signals
each of your repos produces, and hands them to the decision engine as conviction
context. Each function mirrors the methodology of the corresponding repo so the
live system is consistent with the research repos (not a re-derivation).

  equity-sector-analyzer   -> sector_technicals()   RSI, momentum, Sharpe, rel-strength
  fed-rate-sector-analysis -> fed_regime_context()   current-rate / policy-stance context
  fomc-sentiment-analyzer  -> fomc_sentiment()        FinBERT pre-meeting sentiment proxy
  algo-trading-system      -> algo_composite_signal() the composite factor score + golden cross
  rl-portfolio-optimizer   -> (handled in main_engine.load_rl_signal — the decider)

DESIGN: these are CONVICTION / GATING inputs. The RL ensemble still picks the
sector. The repo signals can CONFIRM (boost confidence), CONTRADICT (cut it /
abstain), or provide CONTEXT. This keeps the architecture explainable: one brain,
many corroborating instruments — exactly how a real multi-strategy desk works.

All functions degrade gracefully: if yfinance/network is unavailable they return
None and the engine simply proceeds without that input.
"""

import numpy as np

SECTORS = ["XLK", "XLF", "XLE", "XLV", "XLY", "XLP", "XLI", "XLB", "XLRE", "XLU", "XLC"]


def _safe_download(tickers, period="1y"):
    """Wrapper so the whole module degrades gracefully without yfinance/network."""
    try:
        import yfinance as yf
        data = yf.download(tickers, period=period, progress=False, auto_adjust=True)
        if data is None or len(data) == 0:
            return None
        return data["Close"] if "Close" in data else data
    except Exception as e:
        print(f"[repo_signals] data download unavailable ({e})")
        return None


# ---- equity-sector-analyzer methodology ------------------------------
def _rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def sector_technicals(closes=None):
    """
    Mirrors equity-sector-analyzer: per-sector RSI(14), 20d momentum, 126d rolling
    Sharpe, and 20d relative strength vs SPY. Returns {ticker: {rsi, mom, sharpe, rel}}.
    """
    closes = closes if closes is not None else _safe_download(SECTORS + ["SPY"])
    if closes is None:
        return {}
    out = {}
    spy_ret20 = closes["SPY"].pct_change(20).iloc[-1] if "SPY" in closes else 0
    for t in SECTORS:
        if t not in closes:
            continue
        s = closes[t].dropna()
        if len(s) < 130:
            continue
        rets = s.pct_change().dropna()
        rsi = float(_rsi(s).iloc[-1])
        mom = float(s.pct_change(20).iloc[-1])
        roll = rets.rolling(126)
        sharpe = float((roll.mean() / roll.std()).iloc[-1] * np.sqrt(252))
        rel = float(s.pct_change(20).iloc[-1] - spy_ret20)
        out[t] = {"rsi": round(rsi, 1), "mom": round(mom, 4),
                  "sharpe": round(sharpe, 2), "rel": round(rel, 4)}
    return out


# ---- algo-trading-system methodology ---------------------------------
def algo_composite_signal(closes=None):
    """
    Mirrors algo-trading-system: cross-sectional z-scored composite of
    63d momentum (40%), inverted RSI (30%), 126d rolling Sharpe (30%), gated by a
    200d golden-cross filter. Returns {ticker: {score, eligible}} and the top pick.
    """
    closes = closes if closes is not None else _safe_download(SECTORS, period="2y")
    if closes is None:
        return {"by_sector": {}, "top": None}
    mom, inv_rsi, shp, eligible = {}, {}, {}, {}
    for t in SECTORS:
        if t not in closes:
            continue
        s = closes[t].dropna()
        if len(s) < 210:
            continue
        rets = s.pct_change().dropna()
        mom[t] = float(s.pct_change(63).iloc[-1])
        inv_rsi[t] = float(100 - _rsi(s).iloc[-1])
        roll = rets.rolling(126)
        shp[t] = float((roll.mean() / roll.std()).iloc[-1] * np.sqrt(252))
        sma50 = s.rolling(50).mean().iloc[-1]
        sma200 = s.rolling(200).mean().iloc[-1]
        eligible[t] = bool(s.iloc[-1] > sma200 and sma50 > sma200)

    def z(d):
        vals = np.array(list(d.values()))
        mu, sd = vals.mean(), vals.std() or 1
        return {k: (v - mu) / sd for k, v in d.items()}

    if not mom:
        return {"by_sector": {}, "top": None}
    zm, zr, zs = z(mom), z(inv_rsi), z(shp)
    scores = {t: round(0.4 * zm[t] + 0.3 * zr[t] + 0.3 * zs[t], 3) for t in mom}
    by_sector = {t: {"score": scores[t], "eligible": eligible.get(t, False)} for t in scores}
    elig = {t: v["score"] for t, v in by_sector.items() if v["eligible"]}
    top = max(elig, key=elig.get) if elig else None
    return {"by_sector": by_sector, "top": top}


# ---- fed-rate-sector-analysis context --------------------------------
def fed_regime_context(current_rate=None, last_action=None):
    """
    Mirrors fed-rate-sector-analysis: maps current policy stance to a sector-bias
    label. This is CONTEXT, not a trade trigger. Rate/action passed in (you update
    these when the Fed moves) or read from env. Returns a stance + favored/pressured
    sector lists drawn from the repo's findings (Materials/Industrials rate-sensitive).
    """
    import os
    rate = current_rate if current_rate is not None else float(os.environ.get("FED_RATE", "3.625"))
    action = last_action or os.environ.get("FED_LAST_ACTION", "hold")
    if action == "cut":
        stance = "EASING"
        favored = ["XLK", "XLY", "XLRE", "XLF"]   # rate-sensitive growth/cyclical
        pressured = ["XLP", "XLU"]
    elif action == "hike":
        stance = "TIGHTENING"
        favored = ["XLF", "XLE"]
        pressured = ["XLRE", "XLK", "XLU"]
    else:
        stance = "HOLD"
        favored = []
        pressured = []
    return {"rate": rate, "stance": stance, "favored": favored, "pressured": pressured}


# ---- fomc-sentiment-analyzer proxy -----------------------------------
def fomc_sentiment(news_by_sector=None):
    """
    Mirrors fomc-sentiment-analyzer's PMSI concept at the live level. Full FinBERT
    on Fed minutes runs in that repo; here we expose the market-wide sentiment proxy
    so the engine can read a single 'mood' number. If the news feeder already scored
    sectors, we average those; else returns None.
    """
    if not news_by_sector:
        return None
    vals = list(news_by_sector.values())
    if not vals:
        return None
    pmsi = round(sum(vals) / len(vals), 3)
    regime = "BULLISH" if pmsi > 0.15 else "BEARISH" if pmsi < -0.15 else "NEUTRAL"
    return {"pmsi": pmsi, "regime": regime}


# ---- the aggregator the engine calls ---------------------------------
def collect_all(rl_target, news_by_sector=None, sector_tech=None, algo_signals=None):
    """
    Pull every repo's live signal for the RL's chosen target and return a single
    dict of corroboration the decision engine consumes. Network-free safe.

    sector_tech / algo_signals: pass pre-computed dicts to avoid a duplicate download
    (main_engine computes these once and shares them with the ranker).
    """
    if sector_tech is None or algo_signals is None:
        closes = _safe_download(SECTORS + ["SPY"], period="2y")
        sector_tech = sector_tech or sector_technicals(closes)
        algo_signals = algo_signals or algo_composite_signal(closes)
    fed  = fed_regime_context()
    fomc = fomc_sentiment(news_by_sector)

    t = sector_tech.get(rl_target, {})
    a = algo_signals["by_sector"].get(rl_target, {})
    return {
        "equity_analyzer": t or None,
        "algo_system":     a or None,
        "algo_top_pick":   algo_signals.get("top"),
        "tech_all":        sector_tech,        # full per-sector dict — shared with ranker
        "algo_all":        algo_signals,        # full per-sector dict — shared with ranker
        "fed_context":     fed,
        "fomc_sentiment":  fomc,
        "agreement":       _corroboration(rl_target, t, a, algo_signals.get("top"), fed),
    }


def _corroboration(rl_target, tech, algo, algo_top, fed):
    """
    Turn the multi-repo signals into a single corroboration verdict the engine uses
    as a conviction modifier. Counts how many independent repos AGREE with the RL pick.
    """
    agree, total, notes = 0, 0, []
    if algo_top is not None:
        total += 1
        if algo_top == rl_target:
            agree += 1; notes.append("algo-system top pick matches RL")
        else:
            notes.append(f"algo-system prefers {algo_top}")
    if tech:
        total += 1
        if tech.get("rel", 0) > 0 and tech.get("mom", 0) > 0:
            agree += 1; notes.append("equity-analyzer momentum+rel-strength positive")
        else:
            notes.append("equity-analyzer momentum/rel weak")
    if fed and fed.get("favored"):
        total += 1
        if rl_target in fed["favored"]:
            agree += 1; notes.append(f"fed context favors {rl_target} ({fed['stance']})")
        elif rl_target in fed.get("pressured", []):
            notes.append(f"fed context pressures {rl_target} ({fed['stance']})")
        else:
            notes.append(f"fed context neutral on {rl_target} ({fed['stance']})")
    return {"agree": agree, "total": total, "notes": notes}


if __name__ == "__main__":
    # Offline-safe demo (returns mostly None without network, proving graceful degrade)
    print("collect_all(XLF):")
    import json
    print(json.dumps(collect_all("XLF", news_by_sector={"XLF": 0.4, "XLK": -0.1}), indent=2, default=str))
