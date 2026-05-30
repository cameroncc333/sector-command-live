# Sector Command Live

**A production-deployed autonomous trading system** — running live every trading day, making real investment decisions, and delivering ranked briefings via Telegram.

Built by [Cameron Camarotti](https://github.com/cameroncc333) · High School Senior, Mill Creek High School · Class of 2027 · Founder, [All Around Services](https://allaroundserviceatl.com)

**[Live Dashboard →](https://sector-command-live.vercel.app)** &nbsp;|&nbsp; **[GitHub →](https://github.com/cameroncc333/sector-command-live)**

---

## What This Is

Most quant finance projects are backtests. This one is live.

Every trading day at **9AM, 12PM, and 3:30PM EDT**, an automated pipeline:

1. Pulls live market data — VIX, sector prices, RSI, momentum across all 11 S&P 500 sectors
2. Runs a trained **RL ensemble (PPO + A2C + SAC)** requiring ≥2/3 agent agreement to act
3. Scores live news headlines with **FinBERT NLP** across 15 RSS feeds
4. Computes macro indicators — yield curve spread, DXY, VaR, Kelly criterion
5. Generates a **ranked briefing (Option A/B/C)** with dollar sizing and confidence scores
6. Sends it to Telegram — reply `BUY A`, `SKIP`, or ask a question in plain English
7. **Gemini AI** answers in context with today's live market data
8. Logs every decision to a persistent audit trail visible on the live dashboard

The system has been running in production since May 2026.

---

## Live Dashboard

**URL:** https://sector-command-live.vercel.app

| Tab | What It Shows |
|-----|--------------|
| 📊 Today | RL recommendation, confidence bar, agent votes, VIX regime, news sentiment |
| 🎯 Opportunities | Ranked A/B/C picks with Kelly sizing, paper P&L vs SPY |
| 💼 My Positions | Real-money holdings with live prices and P&L, Alpaca paper account |
| 📋 Journal | Every briefing logged — VIX, regime, your BUY/SKIP decision, full reasoning trace |

---

## System Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    SIGNAL PIPELINE (3×/day)                  │
│                                                              │
│  RL Ensemble (brain)           News Sentiment                │
│   PPO ─┐                       15 RSS feeds                  │
│   A2C ─┼─ ≥2/3 vote required   FinBERT NLP    ──► ±10% conf │
│   SAC ─┘       │               WSJ, Bloomberg,               │
│                ▼               Reuters, etc.                 │
│         Governance Layer                                     │
│         (abstain → SPY)                                      │
│                │                                             │
│                ▼               Macro Layer                   │
│         Decision Engine ◄───── Yield curve (10yr-2yr)        │
│         ticker + confidence    DXY dollar index              │
│         + Kelly sizing         VaR / CVaR                    │
│                │                                             │
│                ▼               Cross-Repo Corroboration      │
│         Telegram Briefing ◄─── algo-trading-system           │
│         (A/B/C + sizes)        fed-rate-sector-analysis      │
│                │                                             │
│                ▼                                             │
│         Alpaca Paper ──────── Dashboard + Journal + Redis     │
└─────────────────────────────────────────────────────────────┘
```

**Key design decisions:**

- **Governance layer**: ≥2/3 RL agent agreement required to act. Agents disagree more than you'd think — this forces discipline. Minority disagreements route to SPY.
- **News cannot change the ticker** — it only modifies confidence ±10 pts. The RL picks the asset; FinBERT tells you how much to trust it.
- **Political disclosures (STOCK Act)** are always research-only and never passed into conviction logic.
- **Paper mode** active by default. Switch to live: set `PAPER_MODE=0`.

---

## Telegram Commands

| Command | What It Does |
|---------|-------------|
| `BUY A` / `BUY B` / `BUY C` | Auto-executes on Alpaca paper + logs decision |
| `BUY XLF` | Buys any specific ticker on paper |
| `SKIP` | Logs that you passed on today's signal |
| `HOLD` | Logs hold decision |
| `STATUS` | Last briefing: date, ticker, confidence |
| `WHY` | Full reasoning trace — every factor that drove the decision |
| `ALPHA` | Top stock picks from the 80-stock cross-sectional factor model |
| `PORTFOLIO` | Real-money holdings and cash position |
| `BALANCE 15000` | Sets investable balance (drives sizing in briefings) |
| `BOUGHT XLE 500` | Logs a $500 real-money position in XLE |
| `BOUGHT XLE 10 48.50 my reason` | Logs 10 shares at $48.50 with a note |
| `SOLD XLE` | Removes XLE from holdings |
| `PERF` | Paper portfolio return vs SPY |
| Any question | *"Should I add to crypto?"* — Gemini answers with today's live data |

---

## Repository Structure

```
sector-command-live/
├── main_engine.py              Entry point — runs all 12 signal stages
├── api/
│   └── webhook.js              Vercel Node.js webhook (Telegram + REST API)
├── engine/
│   ├── decision.py             GRPO-normalized scoring, governance layer
│   ├── performance_tracker.py  Paper P&L vs SPY benchmark
│   ├── position_tracker.py     Real-money holdings (SQLite + Redis sync)
│   ├── alpaca_executor.py      Alpaca paper trading API
│   ├── equity_alpha.py         80-stock cross-sectional factor model
│   ├── risk_metrics.py         VaR, CVaR, Kelly, yield curve, DXY
│   ├── sell_signals.py         Trailing stop, RSI overbought, momentum flip
│   └── earnings_calendar.py    Sector ETF earnings event alerts
├── feeders/
│   ├── sector_feeder.py        SPDR ETF prices, RSI, momentum
│   ├── news_feeder.py          15 RSS feeds → FinBERT NLP sentiment
│   ├── crypto_feeder.py        BTC/ETH + GLD/TLT/QQQ via yfinance
│   └── fomc_live_feeder.py     Fed statement scraping + FinBERT-FOMC
├── interface/
│   └── telegram_bot.py         Briefing formatter, all Telegram commands
├── docs/
│   ├── index.html              Live dashboard (GitHub Pages)
│   ├── briefing.json           Latest signal (auto-committed 3×/day)
│   └── journal.json            Full decision history
├── .github/workflows/
│   ├── daily-signals.yml       3×/day signal runs (cron-job.org triggered)
│   ├── event-alerts.yml        Earnings/sell-signal watcher
│   └── weekly-report.yml       Sunday summary
├── generate_rl_signal.py       Run locally → commits updated RL signal
└── vercel.json                 Routes /cron/* → webhook → GitHub Actions
```

---

## Data Flow

```
cron-job.org (9AM/12PM/3:30PM EDT)
    → /cron/briefing (Vercel)
    → GitHub Actions: main_engine.py
        → Redis: read balance/holdings
        → yfinance: live prices, VIX
        → FinBERT: news sentiment
        → RL ensemble: sector pick
        → Alpaca API: paper execute
        → Telegram: send briefing
        → Redis: write sc:last_briefing
        → Git commit: briefing.json, journal.json

Dashboard (GitHub Pages)
    → reads briefing.json (static, updated 3×/day)
    → reads /api/webhook (live: Redis balance, Yahoo Finance prices, Alpaca positions)
    → overlays: live holdings + P&L, live Alpaca account, live journal replies
```

---

## Performance Tracking

- **Alpaca paper account** starts at $100,000. Every `BUY` reply auto-executes a market order. Dashboard shows live equity, daily P&L, and total return vs baseline.
- **SPY comparison**: fetched each run so you can see if today's moves beat the market.
- **Journal**: every briefing logged with VIX, regime, RL votes, news headline, and your reply (BUY/SKIP) — visible on the dashboard Journal tab.

---

## This Is the Capstone

This system draws live signals from a network of specialized repositories:

| Repository | Contribution to This System |
|------------|----------------------------|
| [rl-portfolio-optimizer](https://github.com/cameroncc333/rl-portfolio-optimizer) | PPO/A2C/SAC trained agents — make the core daily sector allocation decisions |
| [algo-trading-system](https://github.com/cameroncc333/algo-trading-system) | Momentum/mean-reversion corroboration signal — agrees or disagrees with RL pick |
| [fed-rate-sector-analysis](https://github.com/cameroncc333/fed-rate-sector-analysis) | Fed rate cycle sector rotation — corroborates or flags the RL's sector choice |
| [equity-sector-analyzer](https://github.com/cameroncc333/equity-sector-analyzer) | Cross-sectional factor model — generates individual stock picks sent in briefings |
| [fomc-sentiment-analyzer](https://github.com/cameroncc333/fomc-sentiment-analyzer) | FinBERT-FOMC sentiment — modifies conviction on Fed announcement days |
| [AAS-Pricing-Model](https://github.com/cameroncc333/AAS-Pricing-Model) | Underlying quant research models — origin of the analytical framework |

---

## Setup

### GitHub Secrets (for Actions)

| Secret | Purpose |
|--------|---------|
| `TELEGRAM_TOKEN` | Telegram bot token |
| `TELEGRAM_CHAT_ID` | Your chat ID |
| `ALPACA_API_KEY` | Alpaca paper trading key |
| `ALPACA_SECRET_KEY` | Alpaca paper trading secret |
| `UPSTASH_REDIS_REST_URL` | Upstash Redis REST endpoint |
| `UPSTASH_REDIS_REST_TOKEN` | Upstash Redis auth token |
| `GEMINI_API_KEY` | Google Gemini API |
| `DEFAULT_BALANCE` | Default position sizing balance |
| `NEWSAPI_KEY` | NewsAPI key |

### Vercel Env Vars (for webhook)

Same as above, plus:

| Var | Purpose |
|-----|---------|
| `GITHUB_PAT` | Fine-grained PAT with Actions:write — triggers dashboard refresh |
| `GITHUB_REPO` | `cameroncc333/sector-command-live` |
| `CRON_SECRET` | Secret key validated by /cron/* endpoints |

### Register Telegram webhook (once)
```bash
curl "https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://sector-command-live.vercel.app/api/webhook"
```

### Update RL signal (every 1-2 weeks, from your MacBook)
```bash
cd ~/Downloads/sector-command-live-2
python generate_rl_signal.py
git add data/rl_signal.json && git commit -m "update RL signal" && git push
```

---

*Paper mode active · Production since May 2026 · Built by a high school student*
