# Sector Command Live

**A production-grade quantitative trading orchestrator that fuses deep-RL sector rotation, multi-asset ranking, real-time risk metrics, and conversational AI into one live system — sending daily briefings, interactive commands, and event alerts to Telegram.**

Built by [Cameron Camarotti](https://github.com/cameroncc333) · Founder, [All Around Services](https://allaroundservice.com)

---

## What it is

`sector-command-live` is the capstone orchestrator tying together a **seven-repository quantitative research pipeline**. It does not replace those repos — it consumes their live outputs and adds what individual repos lack: a governance layer, risk management stack, multi-asset ranking, portfolio tracker, and conversational AI interface.

```
RL ensemble (PPO + A2C + SAC) ─────────────────────────────┐
News sentiment (FinBERT / lexicon) ──────────────────────────┤
Cross-repo corroboration (4 quant repos) ───────────────────►  Decision engine
Crypto + macro signals (BTC, ETH, GLD, TLT, QQQ) ──────────┤  (governance + VaR + Kelly)
FOMC live sentiment (FinBERT-FOMC) ─────────────────────────┤       │
VIX / regime / yield curve / DXY ───────────────────────────┘       ▼
                                                          Telegram briefing (Option A/B/C)
                                                                      │
                                              ┌───────────────────────┤
                                              ▼                       ▼
                                       You reply            Gemini AI answers
                                    BUY A / SOLD / RISK   natural-language Qs
                                              │
                                              ▼
                                     SQLite audit log + Alpaca paper trade
```

## Feature overview

### Core intelligence
- **RL ensemble** — PPO, A2C, SAC agents trained with walk-forward validation on 11 SPDR sector ETFs + SPY/BIL abstain actions. Models at `~/Documents/rl-portfolio-optimizer/models-live/`.
- **Governance layer** — hard rules the model cannot override: ≥2/3 agent agreement required, VIX > 35 forces BIL, max 30% single position, paper-mode default.
- **Cross-repo corroboration** — `repo_signals.py` recomputes live signals from all 4 quant repos (equity-sector-analyzer technicals, algo-trading-system composite score, fed-rate-sector-analysis policy context, fomc-sentiment-analyzer mood proxy) and votes on whether they back the RL pick.
- **News conviction modifier** — FinBERT NLP (lexicon fallback) boosts or trims confidence; can trigger abstain but cannot pick a different sector. Keeps the system explainable.

### Risk management (Phase 4)
- **VaR / CVaR (95%)** — per-position and portfolio-level Value-at-Risk and Conditional VaR from 1yr daily returns
- **Kelly criterion** — f* = (p×b − q)/b; half-Kelly applied; derived from RL confidence + momentum + Sharpe
- **Trailing stop (5%)** — fires when position falls 5% below its rolling high
- **RSI overbought** — WATCH above 72, URGENT above 78
- **Momentum flip** — 20-day momentum turns negative on an open position
- **Stale loss flag** — position held at a loss for 45+ days
- **Earnings calendar** — top 3–5 holdings per sector ETF checked 5 days out; urgent alerts within 2 days

### Macro overlay
- **Yield curve** — 10yr minus 2yr spread (^TNX / ^IRX); inversion = recession warning
- **DXY dollar index** — live from DX-Y.NYB; dollar strength context for commodity/international positions
- **Sector rotation heatmap** — 4-week rolling relative strength vs SPY for all 11 ETFs (dashboard)

### Multi-asset ranking (Phase 3)
- **Unified opportunity list** — sectors + crypto (BTC, ETH) + macro hedges (GLD, TLT, QQQ) ranked by composite score
- **Conviction tiers** — HIGH (Aggressive), MEDIUM (Balanced), LOW (Defensive), SPECULATIVE; VIX-adjusted position caps
- **Option A / B / C briefing** — three ranked picks with dollar amounts, Kelly %, and rationale every run

### Interactive Telegram commands
| Command | What it does |
|---|---|
| `BUY A` / `BUY B` / `BUY C` | Execute pick from latest briefing |
| `BUY 1` / `BUY 2` | Execute by rank number |
| `CRYPTO` | Live BTC + ETH mini-briefing |
| `GOLD` | Gold + macro signals |
| `PORTFOLIO` | Real holdings P&L with live prices |
| `BALANCE 12500` | Set investable balance (drives $ sizing) |
| `BOUGHT XLE 5 47.50` | Log position (shares + price) |
| `BOUGHT BTC-USD 500` | Log position (dollar amount) |
| `SOLD XLE` | Remove position from tracker |
| `EXPLAIN XLF` | What is this asset? |
| `HOW MUCH XLF` | Kelly-based sizing guide for ticker |
| `RISK` | Portfolio VaR, CVaR, macro snapshot |
| `PERF` | Alpha vs SPY ghost portfolio |
| `WHY` | Reasoning behind last decision |
| `SKIP` / `HOLD` | Log no-action with reason |
| Any question | Answered by Gemini AI with full market context |

### Conversational AI
- **Gemini 1.5 Pro router** — every inbound message that isn't a known command is answered by Gemini with a full context block injected: VIX, regime, RL signal, ranked picks with dollar amounts, real portfolio holdings. Falls back gracefully without a key.

### Automation
- **4× daily briefings** via GitHub Actions (9am, noon, 3:30pm, 4:30pm EDT)
- **30-minute event alerts** during market hours (VIX spike, regime flip, position drawdown, sell signals, earnings proximity)
- **Sunday weekly report** — auto-generated HTML with Chart.js, committed to `data/reports/`
- **Live web dashboard** — dark-theme Flask app with 9 auto-refreshing panels (deployed to Vercel)
- **Alpaca paper trading** — real paper orders at https://paper-api.alpaca.markets
- **SQLite journal** with optional Google Sheets mirror

## Architecture (and the reasoning)

- **RL is the decider.** The PPO/A2C/SAC ensemble picks the target sector. Nothing else overrides the *choice* of sector.
- **News is a conviction modifier.** Per-sector FinBERT sentiment can boost confidence, trim it, or trigger an abstain — but cannot pick a different sector. Keeps the system explainable and defensible.
- **SPY / BIL abstain actions.** When agents disagree the system abstains to SPY (broad market); when VIX crosses the crisis threshold it forces BIL (cash). The model can say "no conviction."
- **Politics is research-only.** Congressional disclosures (STOCK Act) are logged as context and walled off from the decision path — the ~45-day legal delay makes them economically useless as a signal. This is enforced in code; political data never enters `decision.py` conviction logic.
- **Risk-first sizing.** Every pick carries a Kelly fraction (derived from live signals), VaR exposure, and earnings proximity warning. Sizing is never arbitrary.
- **All 7 repos feed one decision.** `repo_signals.py` computes live signals using the exact methodology of each upstream repo and produces a cross-repo agreement count that modifies conviction. The RL agent still picks the sector; the other systems vote on whether they back it.

## Quickstart

```bash
git clone https://github.com/cameroncc333/sector-command-live.git
cd sector-command-live
pip install -r requirements-full.txt
python main_engine.py --dry-run     # runs end-to-end, no network sends
```

The dry run works with no API keys (news falls back to a built-in lexicon scorer; RL signal uses a clearly-marked stub). Add keys to go live.

## Configuration (GitHub Secrets)

| Secret | Required | Purpose |
|---|---|---|
| `TELEGRAM_TOKEN` | **yes** | Bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | **yes** | Your chat ID |
| `ALPACA_API_KEY` | **yes** | Paper trading API key |
| `ALPACA_SECRET_KEY` | **yes** | Paper trading secret |
| `GEMINI_API_KEY` | **yes** | Conversational AI router |
| `NEWSAPI_KEY` | optional | Better headlines than RSS fallback |
| `QUIVER_API_KEY` | optional | Congressional disclosures (research-only) |
| `GOOGLE_CREDS_B64` / `SHEET_ID` | optional | Mirror log to Google Sheets |

## Layout

```
sector-command-live/
├── main_engine.py              # orchestrator: collect → decide → rank → notify → log
├── webhook.py                  # Flask: dashboard + all Telegram command handlers
├── engine/
│   ├── decision.py             # governance, abstain logic, news + cross-repo modifiers
│   ├── repo_signals.py         # LIVE signals from all 7 repos → corroboration verdict
│   ├── multi_asset_ranker.py   # unified sector + crypto + macro opportunity list
│   ├── position_tracker.py     # SQLite-backed real holdings, balance, P&L
│   ├── risk_metrics.py         # VaR, CVaR, Kelly, Sharpe, yield curve, DXY, rotation
│   ├── sell_signals.py         # trailing stop, RSI, momentum flip, stale loss
│   ├── earnings_calendar.py    # upcoming earnings for held sector ETFs
│   ├── llm_router.py           # Gemini 1.5 Pro with full market context injection
│   ├── alpaca_executor.py      # Alpaca paper trading API
│   ├── options_overlay.py      # Black-Scholes protective put when VIX > 22
│   ├── performance_tracker.py  # paper P&L + alpha vs SPY ghost portfolio
│   ├── event_alerts.py         # lightweight 30-min watcher (VIX, regime, sell, earnings)
│   ├── report_generator.py     # weekly HTML report with Chart.js
│   └── journal.py              # SQLite + optional Sheets audit log
├── feeders/
│   ├── crypto_feeder.py        # BTC, ETH + GLD, TLT, QQQ signals
│   ├── news_feeder.py          # 7 RSS feeds + FinBERT/lexicon sentiment
│   ├── fomc_live_feeder.py     # live Fed statements scored with FinBERT-FOMC
│   └── political_feeder.py     # STOCK Act disclosures (research-only, never a signal)
├── interface/
│   └── telegram_bot.py         # format_briefing, all command parsers, Gemini Q&A
├── templates/
│   └── dashboard.html          # dark-theme live dashboard (9 auto-refresh panels)
├── core_quant_lib/             # shared math utilities
├── .github/workflows/          # 4×/day signals + 30-min alerts + Sunday report
├── SUMMARY.md                  # 7-repo ecosystem map + data-flow diagram
└── RESEARCH_JOURNAL.md         # design decisions, rejected hypotheses, failures
```

## Live data sources

| Source | Ticker/Feed | What it drives |
|---|---|---|
| yfinance | All sector ETFs, SPY, BIL | RL features, momentum, RSI, rotation heatmap |
| yfinance | ^VIX | Regime classification, position caps |
| yfinance | ^TNX, ^IRX | Yield curve spread |
| yfinance | DX-Y.NYB | Dollar index (DXY) |
| yfinance | BTC-USD, ETH-USD, GLD, TLT, QQQ | Crypto + macro signals |
| federalreserve.gov | Fed statements | FOMC live sentiment (FinBERT-FOMC) |
| RSS (7 feeds) | WSJ, CNBC, Yahoo, Reuters, NYT… | News sentiment per sector |
| Alpaca paper API | Live portfolio | Paper P&L, position tracking |

## Scope and disclaimer

Runs in **paper mode** by default. Cameron manually flips the flag after 30 days of live validation. At the intended account size the trading P&L is immaterial — the value is a rigorous, documented, live decision process. *Not financial advice. Built for analytical and educational purposes.*
