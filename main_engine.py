"""
main_engine.py — Sector Command Live orchestrator

Entry point for the daily GitHub Actions run (6×/day). Flow:
  1. Load RL ensemble signal       rl-portfolio-optimizer models
  2. Pull news sentiment (real)     feeders/news_feeder.py
  3. Pull political (research-only) feeders/political_feeder.py
  4. Multi-repo corroboration       engine/repo_signals.py
  5. Crypto + macro signals         feeders/crypto_feeder.py
  6. Multi-asset ranking            engine/multi_asset_ranker.py
  7. Performance tracker            engine/performance_tracker.py
  8. FOMC live check                feeders/fomc_live_feeder.py
  9. Options overlay                engine/options_overlay.py
 10. Run decision pipeline          engine/decision.py
 11. Send ranked Telegram briefing  interface/telegram_bot.py
 12. Log to SQLite + Sheets         engine/journal.py
 13. Save data/last_briefing.json   (used by webhook for BUY 1 / HOW MUCH)

Run modes:
  python main_engine.py            full run
  python main_engine.py --dry-run  no network sends, prints to console
"""

import os
import sys
import json
import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from feeders.news_feeder        import NewsFeeder
from feeders.political_feeder   import PoliticalFeeder
from feeders.fomc_live_feeder   import get_fomc_conviction
from feeders.crypto_feeder      import get_crypto_signals
from engine.decision            import MarketState, Governance, decide
from engine.journal             import Journal
from engine.repo_signals        import collect_all
from engine.performance_tracker import PaperPortfolio
from engine.options_overlay     import suggest_hedge
from engine.position_tracker    import PositionTracker
from engine.risk_metrics        import macro_snapshot, portfolio_var, format_risk_block, kelly_for_opportunity
from engine.earnings_calendar   import earnings_warning_for_briefing
from engine.sell_signals        import check_exit_signals, format_sell_alerts
import engine.multi_asset_ranker as ranker
from interface.telegram_bot     import TelegramBot

try:
    from feeders.options_feeder import get_options_sentiment, pcr_confidence_modifier
except Exception as _oe:
    print(f"[main_engine] options_feeder unavailable: {_oe}")
    get_options_sentiment = None
    pcr_confidence_modifier = None


def load_rl_signal():
    """
    Load the RL ensemble signal. Reads data/rl_signal.json written by
    rl-portfolio-optimizer/generate_rl_signal.py. Falls back to a stub.
    """
    rl_path = os.environ.get("RL_SIGNAL_JSON",
                             os.path.join(os.path.dirname(__file__), "data", "rl_signal.json"))
    if rl_path and os.path.exists(rl_path):
        with open(rl_path) as f:
            sig = json.load(f)
            sig.setdefault("_source", "LIVE")
            return sig
    return {
        "votes": {"PPO": "BUY XLK", "A2C": "BUY XLK", "SAC": "HOLD"},
        "target": "XLK", "action": "BUY", "confidence": 72,
        "current_weight": 12.0, "rsi": 48.0, "rel_strength": 0.8,
        "vix": 19.5, "regime": "NORMAL", "ghost_alpha": 0.0,
        "_source": "STUB",
    }


def run(dry_run=False):
    today = datetime.date.today().isoformat()
    # PAPER_MODE=0 or PAPER_MODE=false disables paper mode (flip after 30-day track record)
    _paper = os.environ.get("PAPER_MODE", "1").strip().lower() not in ("0", "false", "no")
    gov   = Governance(paper_mode=_paper)

    # 1) RL signal
    rl = load_rl_signal()

    # 2) News (real conviction signal)
    news = NewsFeeder().daily_sector_sentiment(limit=60)

    # 2b) StockTwits social sentiment for the RL target (top 3 holdings of that sector)
    social_sentiment = {}
    try:
        from feeders.news_feeder import get_stocktwits_sentiment
        from engine.earnings_calendar import SECTOR_HOLDINGS
        top_holdings = SECTOR_HOLDINGS.get(rl.get("target", ""), [])[:3]
        for tkr in top_holdings:
            st = get_stocktwits_sentiment(tkr)
            if st:
                social_sentiment[tkr] = st
    except Exception as e:
        print(f"[main_engine] StockTwits skipped ({e})")

    # 3) Political (research-only — never enters decision logic)
    pol           = PoliticalFeeder()
    political_note = pol.briefing_note(rl["target"])

    # 4) Multi-repo corroboration — computes sector_technicals + algo_composite once.
    # The full per-sector dicts are stored in repo_corr["tech_all"] / ["algo_all"] so
    # the ranker can reuse them without a second yfinance download.
    repo_corr = collect_all(rl["target"], news_by_sector=news["by_sector"])

    # 5) Crypto + macro signals
    crypto_signals = {}
    try:
        crypto_signals = get_crypto_signals()
    except Exception as e:
        print(f"[main_engine] crypto feeder skipped ({e})")

    # 6) Multi-asset ranking — the new ranked opportunity list
    pt      = PositionTracker()
    balance = pt.get_balance()
    ranked_ops = []
    try:
        ops = ranker.rank(
            rl_signal      = rl,
            sector_tech    = repo_corr.get("tech_all"),   # reuse — no second download
            algo_signals   = repo_corr.get("algo_all"),   # reuse — no second download
            crypto_signals = crypto_signals,
            news_by_sector = news["by_sector"],
            vix            = rl["vix"],
            balance        = balance,
            max_results    = 5,
        )
        # Serialize to plain dicts for JSON storage
        ranked_ops = [
            {
                "ticker":          o.ticker,
                "name":            o.name,
                "asset_type":      o.asset_type,
                "conviction":      o.conviction,
                "score":           o.score,
                "rationale":       o.rationale,
                "suggested_pct":   o.suggested_pct,
                "suggested_dollar": o.suggested_dollar,
                "signal_summary":  o.signal_summary,
            }
            for o in ops
        ]
    except Exception as e:
        print(f"[main_engine] multi-asset ranker skipped ({e})")

    # 6b) Equity alpha — individual stock picks via cross-sectional factor model
    equity_alpha_picks = []
    try:
        from engine.equity_alpha import get_equity_alpha_picks
        equity_alpha_picks = get_equity_alpha_picks(
            top_n=8,
            rl_sector=rl.get("target"),
            balance=balance,
            regime=rl.get("regime", "NORMAL"),
            vix=float(rl.get("vix", 20.0)),
        )
    except Exception as e:
        print(f"[main_engine] equity alpha skipped ({e})")

    # 6c) Equity alpha conviction-drop exit signals
    # Compare current picks vs. previous run (stored in Redis) to catch when a
    # HIGH/MEDIUM conviction stock degrades. These fire in the Telegram briefing
    # so Cameron knows to review/exit positions in those individual stocks.
    equity_alpha_exit_alerts = []
    try:
        _ru = os.environ.get("UPSTASH_REDIS_REST_URL", "")
        _rt = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
        if _ru and _rt and equity_alpha_picks:
            import requests as _req
            _prev_r = _req.post(_ru, json=["GET", "sc:equity_alpha_prev"],
                                headers={"Authorization": f"Bearer {_rt}"}, timeout=3)
            _prev_raw = _prev_r.json().get("result")
            if _prev_raw:
                prev_by_ticker = {p["ticker"]: p for p in json.loads(_prev_raw)}
                for pick in equity_alpha_picks:
                    t = pick.get("ticker")
                    prev = prev_by_ticker.get(t)
                    if not prev:
                        continue
                    prev_conv = prev.get("conviction", "")
                    curr_conv = pick.get("conviction", "")
                    sc = pick.get("composite_score", 0)
                    tag = pick.get("conviction_tagline") or "Factors deteriorated"
                    if prev_conv in ("HIGH", "MEDIUM") and curr_conv == "AVOID":
                        equity_alpha_exit_alerts.append({
                            "ticker": t, "signal_type": "CONVICTION_DROP",
                            "urgency": "URGENT",
                            "detail": (f"Alpha conviction {prev_conv} → AVOID (score {sc:.0f}). "
                                       f"{tag}"),
                        })
                    elif prev_conv == "HIGH" and curr_conv == "LOW":
                        equity_alpha_exit_alerts.append({
                            "ticker": t, "signal_type": "CONVICTION_DROP",
                            "urgency": "WATCH",
                            "detail": (f"Alpha conviction HIGH → LOW (score {sc:.0f}). "
                                       f"Monitor position."),
                        })
            # Persist minimal snapshot for next run's comparison (7-day TTL)
            _req.post(_ru, json=["SETEX", "sc:equity_alpha_prev", 604800,
                                 json.dumps([{
                                     "ticker": p.get("ticker"),
                                     "conviction": p.get("conviction"),
                                     "composite_score": p.get("composite_score", 0),
                                     "conviction_tagline": p.get("conviction_tagline", ""),
                                 } for p in equity_alpha_picks], default=str)],
                      headers={"Authorization": f"Bearer {_rt}"}, timeout=3)
    except Exception as e:
        print(f"[main_engine] equity alpha exit check skipped ({e})")

    # 7) Paper performance — restore Redis replies first so BUY decisions are visible
    ghost_alpha = rl.get("ghost_alpha", 0.0)
    perf_data   = None
    try:
        Journal()._restore_replies_from_redis()
    except Exception as e:
        print(f"[main_engine] pre-performance reply restore skipped ({e})")
    try:
        pp          = PaperPortfolio()
        ghost_alpha = pp.ghost_alpha()
        perf_data   = pp.compute()
    except Exception as e:
        print(f"[main_engine] performance tracker skipped ({e})")

    # 8) FOMC live check
    fomc_live = {"active": False}
    try:
        fomc_live = get_fomc_conviction()
    except Exception as e:
        print(f"[main_engine] FOMC live feeder skipped ({e})")

    # 8b) Options sentiment (PCR) — market-wide + RL sector
    options_signals = {}
    try:
        if get_options_sentiment:
            spy_qqq = get_options_sentiment(["SPY", "QQQ"])
            options_signals = spy_qqq
    except Exception as e:
        print(f"[main_engine] options feeder skipped ({e})")

    # 9) Options overlay
    hedge = {"triggered": False}
    try:
        hedge = suggest_hedge(rl["target"], rl["vix"], rl["regime"])
    except Exception as e:
        print(f"[main_engine] options overlay skipped ({e})")

    # 10) Macro indicators (yield curve, dollar)
    macro = {}
    try:
        macro = macro_snapshot()
    except Exception as e:
        print(f"[main_engine] macro snapshot skipped ({e})")

    # 11) Earnings calendar — warn if held/recommended sector has earnings soon
    earnings_warning = ""
    raw_earnings = []
    try:
        held_sectors = [h["ticker"] for h in pt.get_holdings() if h["ticker"] in [
            "XLK","XLF","XLE","XLV","XLY","XLP","XLI","XLB","XLRE","XLU","XLC"]]
        earnings_warning, raw_earnings = earnings_warning_for_briefing(rl["target"], held_sectors)
    except Exception as e:
        print(f"[main_engine] earnings calendar skipped ({e})")

    # 12) Sell signals — check real holdings for exit triggers
    sell_alerts_text = ""
    sell_signals_raw = []
    try:
        holdings_list = pt.get_holdings()
        sell_signals_raw = check_exit_signals(holdings_list)
        if sell_signals_raw:
            sell_alerts_text = format_sell_alerts(sell_signals_raw)
    except Exception as e:
        print(f"[main_engine] sell signals skipped ({e})")

    # 13) VaR / CVaR for real holdings
    var_data = {}
    try:
        if pt.get_holdings():
            var_data = portfolio_var(pt.get_holdings())
    except Exception as e:
        print(f"[main_engine] VaR skipped ({e})")

    # 14) Kelly fractions on ranked ops (attach to each opportunity)
    for opp in ranked_ops:
        try:
            opp["kelly_pct"] = round(kelly_for_opportunity(opp) * 100, 1)
        except Exception:
            pass

    # Data freshness stamp — VIX term structure enriches regime label
    vts = macro.get("vix_term_structure", {})
    vts_regime = vts.get("ts_regime", "")
    freshness = {
        "rl_source":    rl.get("_source", "STUB"),
        "market_data":  "LIVE" if repo_corr.get("equity_analyzer") else "UNAVAILABLE",
        "news":         news["mode"].upper(),
        "generated_utc": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "vix_ts":       vts_regime or "—",
    }

    # 10) Decision engine
    state = MarketState(
        date           = today,
        vix            = rl["vix"],
        regime         = rl["regime"],
        rl_votes       = rl["votes"],
        rl_target      = rl["target"],
        rl_action      = rl["action"],
        rl_confidence  = rl["confidence"],
        current_weight = rl.get("current_weight", 0.0),
        rsi            = rl.get("rsi"),
        rel_strength   = rl.get("rel_strength"),
        news_by_sector = news["by_sector"],
        news_headline  = news["top_headline"],
        political_note = political_note,
        ghost_alpha    = ghost_alpha,
        repo_corroboration = repo_corr.get("agreement"),
        vix_ts_regime  = vts_regime or None,
        options_signals = options_signals or None,
    )
    briefing = decide(state, gov)

    # Portfolio snapshot — read directly from Redis (authoritative for Telegram-set data),
    # then fetch live prices via yfinance so current_price is never stale.
    portfolio_snap = {}
    try:
        import requests as _pf_req
        _ru = os.environ.get("UPSTASH_REDIS_REST_URL", "")
        _rt = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
        _r_bal = None
        _r_hld = []
        if _ru and _rt:
            _br = _pf_req.post(_ru, json=["GET", "sc:balance"],
                               headers={"Authorization": f"Bearer {_rt}"}, timeout=3)
            _bv = _br.json().get("result")
            if _bv is not None:
                _r_bal = float(_bv)
            _hr = _pf_req.post(_ru, json=["GET", "sc:holdings"],
                               headers={"Authorization": f"Bearer {_rt}"}, timeout=3)
            _hv = _hr.json().get("result")
            if _hv:
                _r_hld = json.loads(_hv) if isinstance(_hv, str) else (_hv or [])
        # Fall back to position_tracker (reads SQLite then Redis)
        if _r_bal is None:
            _r_bal = pt.get_balance()
        if not _r_hld:
            _r_hld = pt.get_holdings()
        if _r_bal:
            _tickers = [h["ticker"] for h in _r_hld if h.get("ticker")]
            _prices = {}
            if _tickers:
                try:
                    import yfinance as _yf2
                    _pdata = _yf2.download(_tickers, period="2d", progress=False, auto_adjust=True)
                    for _t in _tickers:
                        try:
                            _col = _pdata["Close"] if len(_tickers) > 1 else _pdata["Close"]
                            _px = float(_col[_t].dropna().iloc[-1]) if len(_tickers) > 1 else float(_col.dropna().iloc[-1])
                            _prices[_t] = round(_px, 2)
                        except Exception:
                            pass
                except Exception as _pe:
                    print(f"[main_engine] price fetch failed ({_pe})")
            _invested = 0
            _hld_out = []
            for _h in _r_hld:
                _t = _h.get("ticker", "")
                _dv = _h.get("dollar_value") or 0
                _cp = _prices.get(_t)
                _shares = _h.get("shares")
                _ac = _h.get("avg_cost")
                if _shares and _ac and _cp:
                    _cb = round(_shares * _ac, 2)
                    _cv = round(_shares * _cp, 2)
                    _pnl = round(_cv - _cb, 2)
                    _pnl_pct = round(_pnl / _cb * 100, 2) if _cb else 0
                else:
                    _cb = _dv
                    _pnl = 0
                    _pnl_pct = 0
                _invested += _cb
                _hld_out.append({
                    "ticker": _t,
                    "date_bought": _h.get("date_bought"),
                    "shares": _shares,
                    "avg_cost": _ac,
                    "current_price": _cp,
                    "cost_basis": _cb,
                    "pnl_dollar": _pnl,
                    "pnl_pct": _pnl_pct,
                    "alloc_pct": round(_dv / _r_bal * 100, 1) if _r_bal else None,
                    "notes": _h.get("notes"),
                })
            portfolio_snap = {
                "balance": _r_bal,
                "holdings": _hld_out,
                "total_invested": round(_invested, 2),
                "total_current": round(sum(h["cost_basis"] + h["pnl_dollar"] for h in _hld_out), 2),
                "unrealized_pnl": round(sum(h["pnl_dollar"] for h in _hld_out), 2),
                "unrealized_pct": round(sum(h["pnl_dollar"] for h in _hld_out) / _invested * 100, 2) if _invested else 0,
                "cash_remaining": round(_r_bal - _invested, 2),
            }
    except Exception as _pe2:
        print(f"[main_engine] Redis portfolio snapshot failed ({_pe2}), falling back")
        try:
            portfolio_snap = pt.portfolio_summary()
        except Exception:
            portfolio_snap = {}

    # Alpaca paper account — live equity + positions from broker
    alpaca_paper = {}
    try:
        from engine.alpaca_executor import portfolio_summary as _alpaca_summary
        alpaca_paper = _alpaca_summary()
    except Exception as e:
        print(f"[main_engine] Alpaca paper summary skipped ({e})")
    # Add SPY daily return so dashboard can show Alpaca vs SPY comparison
    try:
        import yfinance as _yf
        _spy = _yf.download("SPY", period="5d", progress=False, auto_adjust=True)
        if len(_spy) >= 2:
            _spy_ret = float(_spy["Close"].iloc[-1] / _spy["Close"].iloc[-2] - 1) * 100
            alpaca_paper["spy_daily_pct"] = round(_spy_ret, 3)
            # Total return since Alpaca account inception (starting $100k)
            _alp_eq = alpaca_paper.get("equity")
            if _alp_eq:
                alpaca_paper["total_return_pct"] = round((_alp_eq / 100000 - 1) * 100, 3)
    except Exception as e:
        print(f"[main_engine] SPY daily return skipped ({e})")

    briefing.update({
        "freshness":            freshness,
        "repo_detail":          repo_corr,
        "performance":          perf_data,
        "fomc_live":            fomc_live,
        "hedge_suggestion":     hedge,
        "ranked_opportunities": ranked_ops,
        "crypto_signals":       crypto_signals,
        "macro":                macro,
        "earnings_warning":     earnings_warning,
        "sell_alerts":          sell_alerts_text,
        "var_data":             var_data,
        "portfolio_snap":       portfolio_snap,
        "equity_alpha_picks":        equity_alpha_picks,
        "equity_alpha_exit_alerts":  equity_alpha_exit_alerts,
        "options_signals":           options_signals,
        "social_sentiment":          social_sentiment,
        "alpaca_paper":              alpaca_paper,
    })

    # 11) Notify
    bot = TelegramBot()
    if dry_run:
        from interface.telegram_bot import format_briefing
        print(format_briefing(briefing))
    else:
        bot.send_briefing(briefing)

    # 12) Log — replay any human replies stored via Vercel webhook before writing
    journal = Journal()
    journal._restore_replies_from_redis()
    journal.log_decision(briefing, research_context={
        "news_mode":              news["mode"],
        "news_headlines_scanned": news["n_headlines"],
        "political_summary":      pol.sector_disclosure_summary(),
        "repo_corroboration":     repo_corr,
        "freshness":              freshness,
        "ranked_opportunities":   ranked_ops,
        "macro":                  macro,
        "sell_signals":           sell_signals_raw,
        "earnings":               raw_earnings,
        "equity_alpha_exits":     equity_alpha_exit_alerts,
    })

    # 13) Persist briefing — JSON file (cold storage) + Redis (live API cache)
    briefing_path = os.path.join(os.path.dirname(__file__), "data", "last_briefing.json")
    os.makedirs(os.path.dirname(briefing_path), exist_ok=True)
    try:
        with open(briefing_path, "w") as f:
            json.dump(briefing, f, indent=2, default=str)
    except Exception as e:
        print(f"[main_engine] could not save last_briefing.json ({e})")

    # Push to Redis so the dashboard/Telegram API never falls back to live compute.
    # Redis is the single source of truth; JSON is the cold backup if Redis is dark.
    try:
        import requests as _req
        _ru = os.environ.get("UPSTASH_REDIS_REST_URL", "")
        _rt = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
        if _ru and _rt:
            _req.post(_ru,
                      json=["SETEX", "sc:last_briefing", 86400,
                            json.dumps(briefing, default=str)],
                      headers={"Authorization": f"Bearer {_rt}"}, timeout=5)
            print("[main_engine] briefing pushed to Redis (24h TTL)")
    except Exception as e:
        print(f"[main_engine] Redis push skipped ({e})")

    return briefing


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    result = run(dry_run=dry)
    print("\n[main_engine] run complete.")
    print(f"  Recommended: {result['action']} {result['ticker']}  ({result['confidence']}%)")
    if result.get("ranked_opportunities"):
        print(f"  Ranked ops: {len(result['ranked_opportunities'])} picks generated")
        for i, o in enumerate(result["ranked_opportunities"], 1):
            t = o.get("ticker") if isinstance(o, dict) else o.ticker
            c = o.get("conviction") if isinstance(o, dict) else o.conviction
            pct = o.get("suggested_pct") if isinstance(o, dict) else o.suggested_pct
            print(f"    {i}. {t} [{c}] {pct:.0f}%")
