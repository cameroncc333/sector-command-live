"""
equity_alpha.py — Cross-sectional factor model for individual stock alpha

Institutional equity alpha funds (Syfe/J.P. Morgan REI, BlackRock BDVEX,
Schroders Global Equity Alpha, Seeking Alpha Alpha Picks) all use the same
core framework: rank individual stocks cross-sectionally by composite factor
scores, then tilt the portfolio toward the highest-scoring names.

This module implements that exact framework for a curated ~80-stock universe
(large-cap S&P 500 names across all 11 sectors):

  Factor          Weight   What it captures
  ──────────────  ──────   ─────────────────────────────────────────────────
  Value           20%      Cheap stocks outperform expensive ones over time.
                           Forward P/E, P/B, P/S (inverted — low is better)
  Quality         25%      Profitable, efficient firms beat the market.
                           ROE, profit margin, revenue growth
  Momentum        30%      Winners keep winning (12-1 month return).
                           12-1 mo price return, 6-mo return, EPS growth
  Technical       15%      Entry timing within the trend.
                           RSI zone (40-65 sweet spot), rel strength vs SPY
  Low Volatility  10%      Lower-risk stocks outperform on a risk-adj basis.
                           Inverted 90-day realized vol, beta

Each factor score is cross-sectionally Z-scored WITHIN its sector so the
model reads pure idiosyncratic alpha, not broad market beta. A tech stock
scoring 85 in Quality beat other tech stocks — not the whole market.

Results are cached in Redis (4-hour TTL) so the engine doesn't re-fetch
80 stocks on every briefing run.
"""

import os
import json
import datetime
import numpy as np
import pandas as pd

# ── Universe ─────────────────────────────────────────────────────────────────
# ~80 liquid large-cap S&P 500 names, balanced across all 11 GICS sectors.
# Chosen for data quality (yfinance coverage) and liquidity (no micro-caps).

EQUITY_UNIVERSE = {
    # Technology
    "AAPL": "XLK",  "MSFT": "XLK",  "NVDA": "XLK",  "AMD": "XLK",
    "AVGO": "XLK",  "ORCL": "XLK",  "CRM": "XLK",   "ADBE": "XLK",
    "QCOM": "XLK",
    # Financials
    "JPM": "XLF",   "BAC": "XLF",   "WFC": "XLF",   "GS": "XLF",
    "MS": "XLF",    "BLK": "XLF",   "V": "XLF",     "MA": "XLF",
    "AXP": "XLF",   "C": "XLF",
    # Health Care
    "LLY": "XLV",   "UNH": "XLV",   "JNJ": "XLV",   "ABBV": "XLV",
    "MRK": "XLV",   "PFE": "XLV",   "TMO": "XLV",   "ABT": "XLV",
    "AMGN": "XLV",  "ISRG": "XLV",
    # Energy
    "XOM": "XLE",   "CVX": "XLE",   "COP": "XLE",   "SLB": "XLE",
    "EOG": "XLE",   "OXY": "XLE",   "PSX": "XLE",
    # Consumer Discretionary
    "AMZN": "XLY",  "TSLA": "XLY",  "HD": "XLY",    "MCD": "XLY",
    "NKE": "XLY",   "SBUX": "XLY",  "LOW": "XLY",   "BKNG": "XLY",
    "TJX": "XLY",
    # Consumer Staples
    "PG": "XLP",    "KO": "XLP",    "PEP": "XLP",   "WMT": "XLP",
    "COST": "XLP",  "PM": "XLP",    "MO": "XLP",
    # Industrials
    "CAT": "XLI",   "HON": "XLI",   "UPS": "XLI",   "RTX": "XLI",
    "GE": "XLI",    "DE": "XLI",    "LMT": "XLI",   "FDX": "XLI",
    # Materials
    "LIN": "XLB",   "APD": "XLB",   "FCX": "XLB",   "NEM": "XLB",
    "SHW": "XLB",   "DOW": "XLB",
    # Communication Services (GICS: GOOGL, META are XLC not XLK)
    "GOOGL": "XLC", "META": "XLC",  "NFLX": "XLC",  "DIS": "XLC",
    "T": "XLC",     "VZ": "XLC",    "CMCSA": "XLC", "CHTR": "XLC",
    # Real Estate
    "AMT": "XLRE",  "PLD": "XLRE",  "EQIX": "XLRE", "SPG": "XLRE",
    "O": "XLRE",
    # Utilities
    "NEE": "XLU",   "DUK": "XLU",   "SO": "XLU",    "D": "XLU",
    "AEP": "XLU",
}

SECTOR_ETF_TO_NAME = {
    "XLK": "Technology",     "XLF": "Financials",      "XLV": "Health Care",
    "XLY": "Consumer Discr.","XLP": "Consumer Staples", "XLE": "Energy",
    "XLI": "Industrials",    "XLB": "Materials",        "XLC": "Communication",
    "XLRE": "Real Estate",   "XLU": "Utilities",
}

# Human-readable company names for the top 80 holdings
STOCK_NAMES = {
    "AAPL": "Apple",          "MSFT": "Microsoft",     "NVDA": "Nvidia",
    "GOOGL": "Alphabet",      "META": "Meta",           "AMZN": "Amazon",
    "AMD":  "AMD",            "AVGO": "Broadcom",       "ORCL": "Oracle",
    "CRM":  "Salesforce",     "ADBE": "Adobe",          "QCOM": "Qualcomm",
    "JPM":  "JPMorgan",       "BAC":  "BofA",           "WFC":  "Wells Fargo",
    "GS":   "Goldman",        "MS":   "Morgan Stanley", "BLK":  "BlackRock",
    "V":    "Visa",           "MA":   "Mastercard",     "AXP":  "AmEx",
    "C":    "Citigroup",
    "LLY":  "Eli Lilly",      "UNH":  "UnitedHealth",  "JNJ":  "J&J",
    "ABBV": "AbbVie",         "MRK":  "Merck",          "PFE":  "Pfizer",
    "TMO":  "Thermo Fisher",  "ABT":  "Abbott",         "AMGN": "Amgen",
    "ISRG": "Intuitive Surg.",
    "XOM":  "ExxonMobil",     "CVX":  "Chevron",        "COP":  "ConocoPhillips",
    "SLB":  "SLB",            "EOG":  "EOG Resources",  "OXY":  "Occidental",
    "PSX":  "Phillips 66",
    "TSLA": "Tesla",          "HD":   "Home Depot",     "MCD":  "McDonald's",
    "NKE":  "Nike",           "SBUX": "Starbucks",      "LOW":  "Lowe's",
    "BKNG": "Booking Hldgs",  "TJX":  "TJX Companies",
    "PG":   "P&G",            "KO":   "Coca-Cola",      "PEP":  "PepsiCo",
    "WMT":  "Walmart",        "COST": "Costco",         "PM":   "Philip Morris",
    "MO":   "Altria",
    "CAT":  "Caterpillar",    "HON":  "Honeywell",      "UPS":  "UPS",
    "RTX":  "RTX Corp",       "GE":   "GE Aerospace",   "DE":   "Deere & Co",
    "LMT":  "Lockheed",       "FDX":  "FedEx",
    "LIN":  "Linde",          "APD":  "Air Products",   "FCX":  "Freeport-McMoRan",
    "NEM":  "Newmont",        "SHW":  "Sherwin-Williams","DOW":  "Dow Inc",
    "NFLX": "Netflix",        "DIS":  "Disney",          "T":    "AT&T",
    "VZ":   "Verizon",        "CMCSA":"Comcast",         "CHTR": "Charter",
    "AMT":  "American Tower", "PLD":  "Prologis",        "EQIX": "Equinix",
    "SPG":  "Simon Property", "O":    "Realty Income",
    "NEE":  "NextEra Energy", "DUK":  "Duke Energy",     "SO":   "Southern Co",
    "D":    "Dominion",       "AEP":  "AEP",
}

# Factor weights — calibrated to institutional alpha fund literature
WEIGHTS = {
    "value":      0.20,
    "quality":    0.25,
    "momentum":   0.30,
    "technical":  0.15,
    "low_vol":    0.10,
}

CACHE_KEY = "sc:equity_alpha"
CACHE_TTL = 4 * 3600  # 4 hours


# ── Redis helper ──────────────────────────────────────────────────────────────

def _redis(cmd):
    url   = os.environ.get("UPSTASH_REDIS_REST_URL", "")
    token = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
    if not url or not token:
        return None
    try:
        import requests as _r
        resp = _r.post(url, json=cmd,
                       headers={"Authorization": f"Bearer {token}"}, timeout=5)
        return resp.json().get("result")
    except Exception:
        return None


def _cache_get():
    raw = _redis(["GET", CACHE_KEY])
    if not raw:
        return None
    try:
        d = json.loads(raw) if isinstance(raw, str) else raw
        # Respect TTL stored inside the payload
        if datetime.datetime.now(datetime.timezone.utc).isoformat() > d.get("_expires", ""):
            return None
        return d.get("picks", [])
    except Exception:
        return None


def _cache_set(picks: list):
    payload = {
        "picks": picks,
        "_expires": (
            datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=CACHE_TTL)
        ).isoformat(),
    }
    _redis(["SETEX", CACHE_KEY, CACHE_TTL, json.dumps(payload, default=str)])


# ── Data fetching ─────────────────────────────────────────────────────────────

def _fetch_prices(tickers: list, period="1y") -> pd.DataFrame:
    """Batch-download adjusted close prices for all tickers at once."""
    import yfinance as yf
    raw = yf.download(tickers, period=period, auto_adjust=True, progress=False)
    if isinstance(raw.columns, pd.MultiIndex):
        close = raw["Close"]
    else:
        close = raw[["Close"]].rename(columns={"Close": tickers[0]})
    return close.ffill().dropna(how="all")


def _fetch_fundamentals(ticker: str) -> dict:
    """Fetch fundamental info for a single ticker. Returns {} on failure."""
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info or {}
        return info
    except Exception:
        return {}


def _fetch_fundamentals_parallel(tickers: list, max_workers: int = 12) -> dict:
    """Fetch .info for all tickers in parallel. ~10x faster than sequential."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    results = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_to_ticker = {pool.submit(_fetch_fundamentals, t): t for t in tickers}
        for future in as_completed(future_to_ticker):
            t = future_to_ticker[future]
            try:
                results[t] = future.result()
            except Exception:
                results[t] = {}
    return results


# ── Factor computation ────────────────────────────────────────────────────────

def _safe(v, default=None):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return default
    return v


def _raw_factors(ticker: str, info: dict, prices: pd.Series,
                 spy_prices: pd.Series = None) -> dict:
    """
    Compute raw (un-normalized) factor values for a single stock.

    Factors added vs v1 (all sourced from institutional fund research):
      div_yield    — dividend yield (BlackRock BDVEX emphasis on income)
      fcf_yield    — free cash flow / market cap (Paradice, Camissa — better than P/S)
      analyst_mean — analyst consensus 1-5 (Seeking Alpha Alpha Picks core signal)
      rel_spy      — 6mo return relative to SPY (technical relative strength)
    """
    f = {}

    # ── VALUE ────────────────────────────────────────────────────────────────
    f["fwd_pe"]    = _safe(info.get("forwardPE") or info.get("trailingPE"))
    f["pb"]        = _safe(info.get("priceToBook"))
    f["ps"]        = _safe(info.get("priceToSalesTrailingTwelveMonths"))
    # Dividend yield: income-focused value signal (BlackRock BDVEX)
    f["div_yield"] = _safe(info.get("dividendYield"))  # will be None for non-payers → 50 percentile
    # FCF yield: fcf / market cap — better quality-value metric (Paradice)
    fcf = _safe(info.get("freeCashflow"))
    mkt = _safe(info.get("marketCap"))
    f["fcf_yield"] = float(fcf / mkt) if (fcf is not None and not np.isnan(float(fcf or 0))
                                          and mkt and mkt > 0) else None

    # ── QUALITY ──────────────────────────────────────────────────────────────
    f["roe"]           = _safe(info.get("returnOnEquity"))
    f["margin"]        = _safe(info.get("profitMargins"))
    f["rev_growth"]    = _safe(info.get("revenueGrowth"))
    f["eps_growth"]    = _safe(info.get("earningsGrowth"))
    # Analyst consensus: 1=Strong Buy … 5=Strong Sell (Seeking Alpha Alpha Picks)
    # Invert so that Strong Buy = high score
    raw_rec = _safe(info.get("recommendationMean"))
    f["analyst_mean"]  = (6.0 - raw_rec) if raw_rec is not None else None  # 1→5, 5→1 after invert
    # Earnings surprise: actual EPS / estimated EPS - 1 (Russell Inv. Q1-Q2 2024 top signal)
    # +5% surprise = meaningful PEAD (Post-Earnings Announcement Drift)
    f["earnings_surprise"] = _safe(info.get("earningsSurprise"))   # e.g. 0.08 = 8% beat

    # ── MOMENTUM ─────────────────────────────────────────────────────────────
    if prices is not None and len(prices) >= 252:
        p = prices.dropna()
        f["mom_12_1"] = float(p.iloc[-21] / p.iloc[-252] - 1) if len(p) >= 252 else None
        f["mom_6m"]   = float(p.iloc[-1]  / p.iloc[-126] - 1) if len(p) >= 126 else None
        f["mom_1m"]   = float(p.iloc[-1]  / p.iloc[-21]  - 1) if len(p) >= 21  else None
    else:
        f["mom_12_1"] = f["mom_6m"] = f["mom_1m"] = None

    # ── TECHNICAL ────────────────────────────────────────────────────────────
    if prices is not None and len(prices) >= 14:
        p     = prices.dropna()
        delta = p.diff()
        gain  = delta.clip(lower=0).rolling(14).mean()
        loss  = (-delta.clip(upper=0)).rolling(14).mean()
        rsi   = float(100 - 100 / (1 + gain.iloc[-1] / (loss.iloc[-1] + 1e-8)))
        f["rsi"] = rsi
        if 40 <= rsi <= 65:
            f["rsi_score"] = 100.0
        elif rsi > 65:
            f["rsi_score"] = max(0.0, 100.0 - (rsi - 65) * 3)
        else:
            f["rsi_score"] = max(0.0, 100.0 - (40 - rsi) * 2)

        # Relative strength vs SPY (Schroders — sector-relative momentum)
        if spy_prices is not None and len(spy_prices) >= 126 and len(p) >= 126:
            stock_6m = float(p.iloc[-1]   / p.iloc[-126]   - 1)
            spy_6m   = float(spy_prices.iloc[-1] / spy_prices.iloc[-126] - 1)
            f["rel_spy"] = stock_6m - spy_6m
        else:
            f["rel_spy"] = None

        # 52-week high proximity: price / 52-week-high (George & Hwang 2004 — one of
        # the most robust technical predictors. High proximity = strong trend + breakout.)
        if len(p) >= 252:
            high_52w = float(p.iloc[-252:].max())
            f["high_52w_prox"] = float(p.iloc[-1] / high_52w) if high_52w > 0 else None
        elif len(p) >= 63:
            high_52w = float(p.iloc[-63:].max())
            f["high_52w_prox"] = float(p.iloc[-1] / high_52w) if high_52w > 0 else None
        else:
            f["high_52w_prox"] = None
    else:
        f["rsi"] = f["rsi_score"] = f["rel_spy"] = f["high_52w_prox"] = None

    # ── LOW VOLATILITY ────────────────────────────────────────────────────────
    if prices is not None and len(prices) >= 90:
        rets = prices.pct_change().dropna()
        f["vol_90d"] = float(rets.rolling(90).std().iloc[-1] * np.sqrt(252))
        f["beta"]    = _safe(info.get("beta"))
    else:
        f["vol_90d"] = f["beta"] = None

    return f


# ── Cross-sectional scoring ───────────────────────────────────────────────────

def _percentile_rank(series: pd.Series, higher_is_better=True) -> pd.Series:
    """Convert raw values to 0-100 percentile scores within the series."""
    ranked = series.rank(pct=True, na_option="keep") * 100
    if not higher_is_better:
        ranked = 100 - ranked
    return ranked


def _sector_pct_rank(series: pd.Series, sector_map: dict,
                     higher_is_better=True) -> pd.Series:
    """
    Percentile rank within each sector group — the institutional standard.

    A tech stock with P/E 28x should compare to other tech stocks (AAPL, NVDA),
    not to banks with P/E 12x. Without this, the model systematically avoids
    growth sectors. Every major factor fund (BlackRock, Schroders, TD Global)
    does within-sector normalization.
    """
    result = pd.Series(dtype=float, index=series.index)
    sectors = pd.Series(sector_map).reindex(series.index)
    for sector in sectors.dropna().unique():
        members = series.index[sectors == sector]
        group   = series[members].dropna()
        if len(group) < 2:
            result[members] = 50.0
        else:
            result[group.index] = _percentile_rank(group, higher_is_better)
    return result.fillna(50.0)


def _score_universe(raw_factors: dict, sector_map: dict = None,
                    weights: dict = None) -> pd.DataFrame:
    """
    Produce composite alpha scores (0-100) for every ticker.

    sector_map: {ticker: sector_etf} enables within-sector normalization.
    Without it falls back to universe-wide ranks.

    Improvements over v1:
      - Sector-neutral ranking (critical — avoids systematically penalizing growth sectors)
      - div_yield added to value (income signal from BlackRock BDVEX)
      - fcf_yield added to value (cash-generation quality from Paradice/Camissa)
      - analyst_mean added to quality (analyst consensus from Seeking Alpha Alpha Picks)
      - rel_spy added to technical (relative-strength timing signal from Schroders)
    """
    df = pd.DataFrame(raw_factors).T

    rank = (lambda col, hib: _sector_pct_rank(df[col], sector_map, hib)
            if (sector_map and col in df.columns)
            else (_percentile_rank(df[col], hib) if col in df.columns else None))

    scores = pd.DataFrame(index=df.index)

    # ── VALUE (lower multiples = better; higher yields = better) ─────────────
    val_parts = []
    for col in ["fwd_pe", "pb", "ps"]:
        s = rank(col, False)
        if s is not None:
            val_parts.append(s)
    # Dividend yield: higher = better (positive signal for income + value)
    # Non-payers get NaN → filled to 50 so they're not penalized
    if "div_yield" in df.columns:
        s = rank("div_yield", True)
        val_parts.append(s * 0.7)  # lower weight — many growth stocks don't pay divs
    if "fcf_yield" in df.columns:
        s = rank("fcf_yield", True)
        if s is not None:
            val_parts.append(s)
    scores["value"] = pd.concat(val_parts, axis=1).mean(axis=1) if val_parts else pd.Series(50.0, index=df.index)

    # ── QUALITY ──────────────────────────────────────────────────────────────
    qual_parts = []
    for col in ["roe", "margin", "rev_growth", "eps_growth"]:
        s = rank(col, True)
        if s is not None:
            qual_parts.append(s)
    # Analyst consensus: Strong Buy = high score (Seeking Alpha Alpha Picks)
    if "analyst_mean" in df.columns:
        s = rank("analyst_mean", True)
        if s is not None:
            qual_parts.append(s * 0.8)
    # Earnings surprise: PEAD (Post-Earnings Announcement Drift) — Russell Inv. top signal.
    # Beats drive sustained drift for 60-90 days. Higher surprise = higher rank.
    if "earnings_surprise" in df.columns:
        s = rank("earnings_surprise", True)
        if s is not None:
            qual_parts.append(s * 1.2)  # upweighted: strongest quality sub-signal in 2024
    scores["quality"] = pd.concat(qual_parts, axis=1).mean(axis=1) if qual_parts else pd.Series(50.0, index=df.index)

    # ── MOMENTUM ─────────────────────────────────────────────────────────────
    mom_parts = []
    for col in ["mom_12_1", "mom_6m"]:
        s = rank(col, True)
        if s is not None:
            mom_parts.append(s)
    if "mom_1m" in df.columns:
        s = rank("mom_1m", False)
        if s is not None:
            mom_parts.append(s * 0.5)
    scores["momentum"] = pd.concat(mom_parts, axis=1).mean(axis=1) if mom_parts else pd.Series(50.0, index=df.index)

    # ── TECHNICAL ─────────────────────────────────────────────────────────────
    tech_parts = []
    if "rsi_score" in df.columns:
        tech_parts.append(df["rsi_score"].fillna(50.0))
    if "rel_spy" in df.columns:
        s = rank("rel_spy", True)
        if s is not None:
            tech_parts.append(s)
    # 52-week high proximity (George & Hwang 2004): stocks near their 52w high
    # tend to continue outperforming — investors anchor to the high as a target.
    if "high_52w_prox" in df.columns:
        s = rank("high_52w_prox", True)
        if s is not None:
            tech_parts.append(s * 1.1)  # slightly upweighted: robust multi-decade signal
    scores["technical"] = pd.concat(tech_parts, axis=1).mean(axis=1) if tech_parts else pd.Series(50.0, index=df.index)

    # ── LOW VOLATILITY ────────────────────────────────────────────────────────
    lv_parts = []
    for col in ["vol_90d", "beta"]:
        s = rank(col, False)
        if s is not None:
            lv_parts.append(s)
    scores["low_vol"] = pd.concat(lv_parts, axis=1).mean(axis=1) if lv_parts else pd.Series(50.0, index=df.index)

    # ── COMPOSITE (regime-adjusted weights if provided, else base WEIGHTS) ───────
    w_map = weights if weights else WEIGHTS
    scores["composite"] = sum(
        scores[f].fillna(50.0) * w for f, w in w_map.items() if f in scores.columns
    )
    return scores


# ── Factor timing ────────────────────────────────────────────────────────────

def _regime_weights(regime: str, vix: float) -> dict:
    """
    Dynamically adjust factor weights based on macro regime.

    Academic backing (all sourced from the factor-timing literature):
      Stressed / high VIX   → Low-Vol + Quality dominate (drawdown protection)
      Bull / calm            → Momentum dominates (trend persistence)
      Rate hike / hawkish    → Value + Quality dominate (duration compression)
      Normal                 → base WEIGHTS (balanced)
    """
    r = str(regime).upper()
    if vix >= 28 or r in ("STRESSED", "CRISIS"):
        return {"value": 0.18, "quality": 0.28, "momentum": 0.17, "technical": 0.12, "low_vol": 0.25}
    if r == "CALM" and vix <= 16:
        return {"value": 0.12, "quality": 0.20, "momentum": 0.42, "technical": 0.18, "low_vol": 0.08}
    if vix > 20 or r in ("HIGH_RATES", "HAWKISH"):
        return {"value": 0.28, "quality": 0.30, "momentum": 0.20, "technical": 0.12, "low_vol": 0.10}
    return dict(WEIGHTS)


# ── Conviction tagline ────────────────────────────────────────────────────────

def _conviction_tagline(factor_scores: dict, sector_name: str) -> str:
    """
    One-sentence thesis automatically generated from the two highest-scoring factors.
    Inspired by Seeking Alpha Alpha Picks explainability notes.
    """
    named = [("value","Value"),("quality","Quality"),("momentum","Momentum"),
             ("technical","Technical"),("low_vol","Low-Vol")]
    ranked = sorted(named, key=lambda x: factor_scores.get(x[0], 50), reverse=True)
    top2   = [label for _, label in ranked[:2] if factor_scores.get(ranked[0][0], 0) >= 62]

    combos = {
        ("Momentum",  "Quality"):   "Quality Momentum — strong earnings + sustained price trend",
        ("Quality",   "Momentum"):  "Quality Momentum — strong earnings + sustained price trend",
        ("Momentum",  "Technical"): "Trend-following setup — price strength + ideal entry timing",
        ("Technical", "Momentum"):  "Trend-following setup — price strength + ideal entry timing",
        ("Quality",   "Low-Vol"):   "Defensive Quality — high ROE with low portfolio volatility",
        ("Low-Vol",   "Quality"):   "Defensive Quality — high ROE with low portfolio volatility",
        ("Value",     "Quality"):   "Quality at a Reasonable Price (QARP)",
        ("Quality",   "Value"):     "Quality at a Reasonable Price (QARP)",
        ("Value",     "Momentum"):  "Value rerating — cheap stock building upward momentum",
        ("Momentum",  "Value"):     "Value rerating — cheap stock building upward momentum",
        ("Quality",   "Technical"): "Quality breakout — strong fundamentals + technical timing",
        ("Technical", "Quality"):   "Quality breakout — strong fundamentals + technical timing",
        ("Momentum",  "Low-Vol"):   "Low-risk momentum — trending with below-average volatility",
        ("Low-Vol",   "Momentum"):  "Low-risk momentum — trending with below-average volatility",
        ("Value",     "Low-Vol"):   "Income/defensive value — cheap + stable",
        ("Low-Vol",   "Value"):     "Income/defensive value — cheap + stable",
    }
    if len(top2) >= 2:
        return combos.get(tuple(top2[:2]), f"{' + '.join(top2[:2])} strength in {sector_name}")
    if top2:
        return f"{top2[0]}-driven pick in {sector_name}"
    return f"Balanced factor profile — {sector_name}"


# ── Conviction mapping ────────────────────────────────────────────────────────

def _conviction(score: float) -> str:
    if score >= 75:
        return "HIGH"
    elif score >= 62:
        return "MEDIUM"
    elif score >= 48:
        return "LOW"
    else:
        return "AVOID"


def _build_rationale(ticker, factors, scores, sector_etf, rl_sector=None):
    """Build a prioritized list of reasons for the rating."""
    reasons = []
    s = scores

    if s.get("momentum", 50) >= 70:
        m = factors.get("mom_12_1")
        if m is not None:
            reasons.append(f"Strong 12mo momentum vs sector peers ({m:+.1%})")
    if s.get("quality", 50) >= 72:
        roe = factors.get("roe")
        mg  = factors.get("margin")
        ac  = factors.get("analyst_mean")
        eps_surp = factors.get("earnings_surprise")
        if eps_surp is not None and eps_surp > 0.03:
            reasons.append(f"Earnings beat {eps_surp:+.1%} vs estimates (PEAD momentum)")
        if roe is not None:
            reasons.append(f"High quality: ROE {roe:.1%}")
        elif mg is not None:
            reasons.append(f"High quality: margin {mg:.1%}")
        if ac is not None and ac >= 4.0:
            reasons.append(f"Analyst consensus: Strong Buy ({6 - ac:.1f}/5)")
    if s.get("value", 50) >= 70:
        pe  = factors.get("fwd_pe")
        fcf = factors.get("fcf_yield")
        dy  = factors.get("div_yield")
        if pe is not None:
            reasons.append(f"Cheap vs sector peers: fwd P/E {pe:.1f}x")
        elif fcf is not None:
            reasons.append(f"High FCF yield ({fcf:.1%}) — cash cow")
        if dy and dy > 0.02:
            reasons.append(f"Dividend yield {dy:.1%}")
    if s.get("technical", 50) >= 78:
        rsi       = factors.get("rsi")
        relspy    = factors.get("rel_spy")
        prox_52w  = factors.get("high_52w_prox")
        if prox_52w is not None and prox_52w >= 0.97:
            reasons.append(f"Near 52-week high ({prox_52w:.1%}) — breakout zone")
        if rsi is not None:
            reasons.append(f"RSI {rsi:.0f} — ideal entry zone")
        if relspy is not None and relspy > 0.05:
            reasons.append(f"Outperforming SPY by {relspy:+.1%} over 6mo")
    if rl_sector and sector_etf == rl_sector:
        reasons.append(f"RL ensemble agrees on {SECTOR_ETF_TO_NAME.get(sector_etf, sector_etf)} sector")
    if not reasons:
        reasons.append(f"Composite alpha score: {s.get('composite', 50):.0f}/100")
    return reasons[:4]


# ── Public API ────────────────────────────────────────────────────────────────

def get_equity_alpha_picks(
    top_n: int = 8,
    rl_sector: str = None,
    balance: float = None,
    regime: str = "NORMAL",
    vix: float = 20.0,
    force_refresh: bool = False,
) -> list[dict]:
    """
    Return the top-N individual stock picks ranked by composite alpha score.

    New params vs v1:
      regime  — RL macro regime label ("NORMAL"/"CALM"/"STRESSED") for factor timing
      vix     — current VIX for factor timing
      Both are used by _regime_weights() to tilt factor weights dynamically.

    Intra-sector diversification: no more than MAX_PER_SECTOR picks from any one
    sector in the returned top_n (avoids 6 semiconductor stocks crowding the list).

    Results are Redis-cached for 4 hours.
    """
    if not force_refresh:
        cached = _cache_get()
        if cached:
            return cached[:top_n]

    print(f"[EquityAlpha] Scoring {len(EQUITY_UNIVERSE)} stocks "
          f"(regime={regime}, VIX={vix:.1f}, parallel fetch)...")

    weights     = _regime_weights(regime, vix)
    tickers     = list(EQUITY_UNIVERSE.keys())
    all_tickers = tickers + ["SPY"]

    # 1. Batch price fetch
    try:
        price_df = _fetch_prices(all_tickers, period="1y")
    except Exception as e:
        print(f"[EquityAlpha] price fetch failed: {e}")
        return []

    spy_prices = price_df["SPY"] if "SPY" in price_df.columns else None

    # 2. Parallel fundamentals fetch (~12x faster than sequential)
    all_info = _fetch_fundamentals_parallel(tickers, max_workers=12)

    # 3. Raw factors per ticker
    raw_factors = {}
    for ticker in tickers:
        prices = price_df[ticker] if ticker in price_df.columns else None
        raw_factors[ticker] = _raw_factors(ticker, all_info.get(ticker, {}),
                                           prices, spy_prices)

    # 4. Sector-neutral cross-sectional scoring with regime-adjusted weights
    all_scores = _score_universe(raw_factors, sector_map=EQUITY_UNIVERSE,
                                 weights=weights)

    # 5. Build full ranked list (excluding AVOID)
    all_picks = []
    for ticker in tickers:
        sector_etf  = EQUITY_UNIVERSE[ticker]
        sector_name = SECTOR_ETF_TO_NAME.get(sector_etf, sector_etf)
        row_scores  = all_scores.loc[ticker].to_dict() if ticker in all_scores.index else {}
        row_factors = raw_factors.get(ticker, {})
        composite   = float(row_scores.get("composite", 50.0))
        conv        = _conviction(composite)
        if conv == "AVOID":
            continue

        pct_map          = {"HIGH": 12.0, "MEDIUM": 7.0, "LOW": 4.0}
        suggested_pct    = pct_map.get(conv, 4.0)
        suggested_dollar = round(balance * suggested_pct / 100, 2) if balance else None
        fs = {
            "value":      round(float(row_scores.get("value",    50)), 1),
            "quality":    round(float(row_scores.get("quality",  50)), 1),
            "momentum":   round(float(row_scores.get("momentum", 50)), 1),
            "technical":  round(float(row_scores.get("technical",50)), 1),
            "low_vol":    round(float(row_scores.get("low_vol",  50)), 1),
        }

        all_picks.append({
            "ticker":             ticker,
            "name":               STOCK_NAMES.get(ticker, ticker),
            "sector_etf":         sector_etf,
            "sector_name":        sector_name,
            "conviction":         conv,
            "composite_score":    round(composite, 1),
            "factor_scores":      fs,
            "conviction_tagline": _conviction_tagline(fs, sector_name),
            "factor_regime":      regime,
            "raw_factors": {
                "fwd_pe":             row_factors.get("fwd_pe"),
                "pb":                 row_factors.get("pb"),
                "div_yield":          row_factors.get("div_yield"),
                "fcf_yield":          row_factors.get("fcf_yield"),
                "roe":                row_factors.get("roe"),
                "margin":             row_factors.get("margin"),
                "analyst_mean":       row_factors.get("analyst_mean"),
                "earnings_surprise":  row_factors.get("earnings_surprise"),
                "mom_12_1":           row_factors.get("mom_12_1"),
                "mom_6m":             row_factors.get("mom_6m"),
                "rel_spy":            row_factors.get("rel_spy"),
                "rsi":                row_factors.get("rsi"),
                "high_52w_prox":      row_factors.get("high_52w_prox"),
                "vol_90d":            row_factors.get("vol_90d"),
            },
            "rationale":       _build_rationale(ticker, row_factors, row_scores,
                                                sector_etf, rl_sector),
            "suggested_pct":    suggested_pct,
            "suggested_dollar": suggested_dollar,
            "asset_type":       "equity",
        })

    all_picks.sort(key=lambda x: x["composite_score"], reverse=True)

    # 6. Enforce intra-sector cap: max MAX_PER_SECTOR picks per sector_etf.
    #    This prevents the list being dominated by (e.g.) 6 semiconductors
    #    when tech is the top RL sector. Overflow stocks fill remaining slots.
    MAX_PER_SECTOR = 2
    sector_count: dict = {}
    diversified:  list = []
    overflow:     list = []
    for p in all_picks:
        sect = p["sector_etf"]
        if sector_count.get(sect, 0) < MAX_PER_SECTOR:
            diversified.append(p)
            sector_count[sect] = sector_count.get(sect, 0) + 1
        else:
            overflow.append(p)
    if len(diversified) < top_n:
        diversified.extend(overflow[:top_n - len(diversified)])
    picks = diversified[:top_n]

    if picks:
        weight_label = (f"regime={regime}" if weights != WEIGHTS else "base weights")
        print(f"[EquityAlpha] Top: {picks[0]['ticker']} "
              f"({picks[0]['conviction']}, {picks[0]['composite_score']}) | {weight_label}")

    _cache_set(picks)
    return picks


def format_equity_alpha_telegram(picks: list, n: int = 5) -> str:
    """Format top N equity alpha picks for Telegram HTML message."""
    if not picks:
        return "No equity alpha picks available. They generate with each daily briefing."
    regime = picks[0].get("factor_regime", "NORMAL") if picks else "NORMAL"
    lines  = [f"📊 <b>Individual Stock Alpha</b> — factor model  [{regime} regime weights]", ""]
    conv_emoji = {"HIGH": "🔥", "MEDIUM": "✅", "LOW": "🟡"}
    for i, p in enumerate(picks[:n], 1):
        emoji    = conv_emoji.get(p.get("conviction"), "⚪")
        score    = p.get("composite_score", 0)
        conv     = p.get("conviction", "LOW")
        name     = p.get("name", p.get("ticker", "?"))
        sector   = p.get("sector_name", "")
        tagline  = p.get("conviction_tagline", "")
        dollar   = f"  → <b>${p['suggested_dollar']:.0f}</b>" if p.get("suggested_dollar") else ""
        fs       = p.get("factor_scores", {})
        rf       = p.get("raw_factors", {})
        factor_line = (f"V:{fs.get('value',0):.0f} "
                       f"Q:{fs.get('quality',0):.0f} "
                       f"M:{fs.get('momentum',0):.0f} "
                       f"T:{fs.get('technical',0):.0f} "
                       f"LV:{fs.get('low_vol',0):.0f}")
        # Extra signal hints inline
        extras = []
        eps_s = rf.get("earnings_surprise")
        if eps_s is not None and abs(eps_s) > 0.02:
            extras.append(f"EPS beat {eps_s:+.1%}")
        prox = rf.get("high_52w_prox")
        if prox is not None and prox >= 0.95:
            extras.append(f"52wk high {prox:.0%}")
        extra_str = f"  <i>{' · '.join(extras)}</i>" if extras else ""
        lines.append(f"  {i}. {emoji} <b>{p['ticker']}</b> {name} ({sector}) "
                     f"— score <b>{score:.0f}</b> [{conv}]{dollar}")
        lines.append(f"     Factors: {factor_line}{extra_str}")
        if tagline:
            lines.append(f"     💡 {tagline}")
    lines.append("")
    lines.append("Reply <code>BOUGHT AAPL 500</code> to log $500 of any stock.")
    return "\n".join(lines)
