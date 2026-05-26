# Research Journal — Sector Command Live

A running record of *why* the system is built the way it is, including the ideas I
**rejected** and the failures I worked through. This is deliberately a separate
document from the code comments — it captures reasoning, not implementation.

---

## Design decisions and why

### Why news sentiment is a *modifier*, not a decider
The temptation was to let news pick trades directly. I rejected that. If news could
choose the sector, the system would just be a headline-following bot, and headlines
are noisy and often already priced in. Instead the RL ensemble — trained on a reward
function that already accounts for risk and regime — makes the call, and news can
only raise or lower conviction or push toward an abstain action. This keeps the
architecture explainable: every recommendation traces back to the agents, with news
as a documented adjustment.

### Why Congressional trading is research-only
I considered wiring Congressional disclosures in as a live signal. I decided against
it on the merits, not just on caution. The STOCK Act permits up to ~45 days before
disclosure, so any informational edge is gone by the time the data is public — using
it as a trade trigger would be following stale information. The genuinely interesting
question is whether disclosed activity correlates with *subsequent* sector returns at
all after the delay. That's a testable hypothesis, and a null result ("no edge
survives the reporting delay") would be a real finding — directly parallel to the
null result in my FOMC sentiment study. So the data is logged as a research label and
walled off from the decision path entirely.

### Why SPY and BIL as abstain actions
The original RL system was structurally forced to stay invested across the 11
sectors — it could not say "I don't have conviction right now." That's a design flaw,
not a feature. Adding SPY (broad-market neutral) and BIL (cash) as explicit abstain
actions lets the system express low conviction (default to SPY when agents disagree)
and defensiveness (default to BIL in a VIX crisis). The key claim to benchmark: does
adding abstain actions improve risk-adjusted return versus the forced-allocation
baseline? That delta is the academic backbone of the whitepaper section.

### Why a governance layer the AI can't override
Institutions are judged on their worst week, not their best month. Hard-coded rules —
max 30% per sector, force cash above VIX 35, require ≥2/3 agent agreement to act —
sit above the model and cannot be overridden by it. This demonstrates risk-aware
engineering rather than return-chasing.

### Why SQLite instead of scattered CSVs (and not DuckDB)
Earlier advice pushed DuckDB. For this data volume (a few decisions per day) that's
over-engineering — SQLite ships with Python, needs zero setup, and is more than fast
enough. I chose the simplest tool that solves the problem. DuckDB would matter at
100k+ rows during heavy backtesting; it doesn't here.

### Why Telegram over Twilio
Twilio toll-free verification failed after multiple attempts and contributes nothing
to the research value. Telegram is free, needs no carrier verification, supports
4096-character detailed briefings (vs. Twilio's 160), and handles two-way replies
natively. The notification channel is isolated in one module so it can be swapped
again later without touching decision logic.

---

## Rejected ideas (and why)
- **LLM "reasoning gatekeeper"** to second-guess each trade — rejected: adds a
  non-deterministic, unexplainable failure point to a system whose entire value is
  explainability.
- **Dollar-neutral long/short for Beta ≈ 0** — rejected: shorting isn't appropriate
  for a small custodial account, and market-neutrality isn't a goal that serves the
  documentation purpose.
- **Bot-detection / "narrative authenticity" scoring on social data** — rejected:
  scope creep on a data source not worth trading on.
- **Merging all six repos into one mega-repo** — rejected: high risk of breaking
  working, individually-impressive repos. Built an orchestrator on top instead and
  documented the unified narrative in `SUMMARY.md`.

---

## Failures and fixes (fill in as they happen)
- *(template)* **What broke:** … **Root cause:** … **Fix:** … **Lesson:** …
- Earlier in the project: a `numpy.datetime64` vs `pandas.Timestamp` mismatch caused
  the backtester to silently execute zero trades. Lesson: type mismatches fail quietly
  in pandas — assert types at boundaries.
- Earlier: look-ahead bias from a 200-day MA without a warm-up window. Lesson: every
  indicator needs an explicit warm-up that's excluded from results.

---

## Future work
- Replace the flat 6 bps transaction cost in the RL environment with a square-root
  market-impact model (cost scales with order size vs. ADV) to stop the agent from
  hallucinating alpha by over-trading.
- Verify and (if confirmed) swap in a FOMC-specialized FinBERT model for the news
  feeder, pending independent confirmation the model exists and benchmarks better
  than `ProsusAI/finbert` on held-out Fed text.
- Cooperative regime-gated agent blending (shift voting weight to a risk-parity solver
  during VIX spikes) instead of single-agent selection.
- Event-triggered alerts (VIX spike, regime change, position −5%) outside the 4
  scheduled runs.
