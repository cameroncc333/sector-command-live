# Sector Command Live

**A production-deployed quantitative trading system** that integrates deep reinforcement learning, real-time NLP sentiment analysis, multi-asset portfolio optimization, and conversational AI into a single live decision engine — running autonomously in production, texting daily trade briefings, and managing real paper positions.

Built by [Cameron Camarotti](https://github.com/cameroncc333)

---

## Overview

Most quantitative finance projects are backtests — historical simulations that look good in a notebook but have never touched live data. This is different.

Sector Command runs **four times every trading day** via automated CI/CD, pulls live market data, runs a trained reinforcement learning ensemble, applies institutional-grade risk management, and delivers a ranked briefing to a Telegram interface where positions can be executed, tracked, and explained in plain English via an integrated large language model. Every decision is logged to a persistent audit trail with full reasoning attached.

The system was built to answer a specific question: *can a systematic, explainable quantitative process — not a black box — outperform a passive SPY allocation while managing real downside risk?* After 30 days of paper trading, paper mode is disabled and the system trades real capital.

---

## Architecture

The design separates the brain (what to buy) from the governance layer (whether to act) from the sizing engine (how much to risk). These are three distinct concerns that most retail systems collapse into one — and that collapse is where edge cases become losses.

```
RL Ensemble ─────────────────────────────────────────────┐
  PPO / A2C / SAC agents                                  │
  Walk-forward validated on 11 SPDR sector ETFs           │
  Abstain actions: SPY (no conviction) / BIL (crisis)     │
                                                          ▼
News Sentiment ────────────────────────────────► Decision Engine
  FinBERT NLP across 7 live RSS feeds                     │
  Conviction modifier — cannot change the RL ticker       │  Governance layer:
  Lexicon fallback when transformer unavailable            │  • ≥ 2/3 agent agreement required
                                                          │  • VIX > 35 → force BIL (cash)
Cross-Repo Corroboration ──────────────────────────────► │  • Max 30% per position
  4 upstream repos re-run live signals                    │  • Paper mode until validated
  Equity technicals, algo composite score,                │
  Fed policy context, FOMC sentiment                      ▼
                                                  Multi-Asset Ranker
FOMC Live Feeder ──────────────────────────────►  Sectors + BTC/ETH + GLD/TLT/QQQ
  Live Fed statements from federalreserve.gov             │
  Scored with FinBERT-FOMC                                ▼
                                                  Risk Stack
Macro Overlay ─────────────────────────────────►  VaR(95%) / CVaR(95%) per position
  Yield curve (10yr − 2yr spread)                 Kelly criterion sizing
  DXY dollar index                                Trailing stop / RSI / momentum / stale-loss
  Sector rotation heatmap (4-week rel. strength)  Earnings proximity warnings
                                                          │
                                                          ▼
                                                  Telegram Briefing
                                                  Option A / B / C ranked picks
                                                  Dollar amounts + Kelly fractions
                                                  Sell alerts + earnings warnings
                                                  Gemini 1.5 Pro answers any question
                                                          │
                                                          ▼
                                                  SQLite Audit Log
                                                  Every decision, every signal, full reasoning
```

---

## What makes it non-trivial

**Reinforcement learning with proper validation.** The PPO, A2C, and SAC agents were trained using walk-forward cross-validation — the same methodology used in institutional backtesting to prevent lookahead bias. Each agent produces a sector allocation; the ensemble requires ≥ 2/3 agreement before acting. Disagreement produces an explicit abstain to SPY rather than a forced trade. Most RL trading projects don't implement abstain actions; this one treats "no conviction" as a first-class output.

**News sentiment that can't override the model.** FinBERT NLP runs across seven live financial RSS feeds and scores sentiment per sector. It can raise or lower confidence, or trigger an abstain — but it cannot change which sector the RL model selected. This constraint is architectural, not advisory. It keeps the system explainable: the model picked the sector; the news said whether to trust it.

**Kelly criterion from live signals.** With no historical win/loss database yet (the system just launched), Kelly fractions are derived analytically: p is bounded RL confidence, b is estimated payoff from momentum magnitude and Sharpe ratio. Half-Kelly is applied. The result is 5–15% typical position fractions that are mathematically consistent with the conviction tier assigned.

**Institutional risk metrics on a student budget.** VaR(95%) and CVaR(95%) are computed per-position from one year of daily returns pulled live. Every briefing includes a trailing stop check (5% from rolling high), RSI overbought detection (watch at 72, urgent at 78), 20-day momentum flip detection, and a stale-loss flag for positions held at a loss for 45+ days. These are the same exit-signal categories used by professional systematic managers, implemented from scratch.

**Congressional trading is walled off by design.** STOCK Act disclosures are logged as research context and are explicitly excluded from the decision path in code — not by convention. The ~45-day legal reporting delay makes them economically useless as a signal, and treating them as one would be intellectually dishonest. The system documents why it ignores them.

**Seven upstream repositories feed one decision.** This repo is the capstone of a seven-project research program built over two years. Rather than duplicating the upstream logic, `repo_signals.py` re-runs the live signal computation from each repo — equity technicals, factor composite scores, Fed policy sensitivity, FOMC mood proxy — and produces a cross-repo agreement count that modifies conviction. The upstream repos remain independent; this one sits on top.

---

## Live data sources

| Source | What it produces |
|---|---|
| Yahoo Finance (yfinance) | All sector ETF prices, VIX, yield curve, DXY, BTC/ETH/GLD/TLT/QQQ |
| 7 financial RSS feeds | Per-sector news sentiment via FinBERT NLP |
| federalreserve.gov | Live FOMC statements scored with FinBERT-FOMC |
| Alpaca paper trading API | Real paper order execution and position tracking |
| SQLite (local) | Persistent journal: decisions, signals, P&L, audit trail |

---

## System behavior

The system runs on a schedule — no human intervention required once deployed.

- **09:00 EDT** — pre-open briefing: overnight signal refresh, macro update, ranked picks with dollar amounts and Kelly fractions
- **12:00 EDT** — midday update: intraday regime check, position monitoring
- **15:30 EDT** — pre-close: sell signal sweep, earnings proximity check
- **16:30 EDT** — EOD summary: P&L update, ghost alpha vs SPY
- **Every 30 minutes** — lightweight event watcher: VIX spike (>20% intraday), regime flip to STRESSED, any position down >5%, sell signals, earnings within 2 days
- **Every Sunday** — auto-generated HTML research report with performance charts

All briefings are delivered to Telegram with full reasoning. Positions can be executed, logged, and queried via text commands. Any question about the market, a ticker, or the system's reasoning is answered by Gemini 1.5 Pro with the full current market state injected as context.

---

## The seven-repository research program

This system is the final layer of a research program that was built bottom-up:

| Repository | What it proved |
|---|---|
| `aas-pricing-model` | A multi-variable calculus cost function (8 inputs, partial-derivative optimization) that prices real service contracts for an active business |
| `fed-rate-sector-analysis` | A statistical event study measuring sector return sensitivity to FOMC rate decisions across 30/60/90-day windows |
| `equity-sector-analyzer` | A 2,000-line live dashboard computing 30+ risk metrics including Black-Scholes Greeks and Fama-French 5-factor decomposition |
| `fomc-sentiment-analyzer` | FinBERT NLP applied to 91 FOMC meeting statements; established the NLP methodology reused in this system's news feeder |
| `algo-trading-system` | A rules-based sector rotation backtester: 238 trades, +64.5% total return, 0.30 Sharpe, −23.5% max drawdown — the baseline the RL agents must beat |
| `rl-portfolio-optimizer` | The RL capstone: PPO/A2C/SAC trained 500,000 steps on 11 sector ETFs with regime-adaptive reward shaping and walk-forward validation |
| **`sector-command-live`** (this repo) | **The production layer: live data, live decisions, live execution, live risk management** |

Each repo is independent and does something real on its own. Together they form a complete quantitative research pipeline — from business math through statistical research through NLP through reinforcement learning through live deployment.

---

## Technical stack

Python · stable-baselines3 (PPO/A2C/SAC) · transformers + FinBERT · Flask · Vercel · GitHub Actions · SQLite · Alpaca Markets API · Google Gemini 1.5 Pro · yfinance · scipy · pandas · numpy

---

*Runs in paper mode during the initial validation period. Not financial advice.*
