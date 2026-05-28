"""
main_engine.py — Sector Command Live orchestrator

Entry point for the daily GitHub Actions run (4×/day). Flow:
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
    gov   = Governance(paper_mode=True)

    # 1) RL signal
    rl = load_rl_signal()

    # 2) News (real conviction signal)
    news = NewsFeeder().daily_sector_sentiment(limit=60)

    # 3) Political (research-only — never enters decision logic)
    pol           = PoliticalFeeder()
    political_note = pol.briefing_note(rl["target"])

    # 4) Multi-repo corroboration
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

    # 7) Paper performance
    ghost_alpha = rl.get("ghost_alpha", 0.0)
    perf_data   = None
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

    # Data freshness stamp
    freshness = {
        "rl_source":    rl.get("_source", "STUB"),
        "market_data":  "LIVE" if repo_corr.get("equity_analyzer") else "UNAVAILABLE",
        "news":         news["mode"].upper(),
        "generated_utc": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
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
    )
    briefing = decide(state, gov)

    # Portfolio snapshot for plain English summary
    portfolio_snap = {}
    try:
        portfolio_snap = pt.portfolio_summary(current_prices={})
    except Exception:
        portfolio_snap = {"balance": balance}

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
    })

    # 11) Notify
    bot = TelegramBot()
    if dry_run:
        from interface.telegram_bot import format_briefing
        print(format_briefing(briefing))
    else:
        bot.send_briefing(briefing)

    # 12) Log
    journal = Journal()
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
    })

    # 13) Save last_briefing.json so webhook can use ranked ops for BUY 1 / HOW MUCH
    briefing_path = os.path.join(os.path.dirname(__file__), "data", "last_briefing.json")
    os.makedirs(os.path.dirname(briefing_path), exist_ok=True)
    try:
        with open(briefing_path, "w") as f:
            # crypto_signals may contain non-serializable floats — use default=str
            json.dump(briefing, f, indent=2, default=str)
    except Exception as e:
        print(f"[main_engine] could not save last_briefing.json ({e})")

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
