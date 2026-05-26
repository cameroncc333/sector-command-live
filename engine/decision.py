"""
decision.py — the brain of Sector Command Live

ARCHITECTURE (the defensible version you can explain in an interview):

  RL ensemble (PPO/A2C/SAC) makes the CALL.
  News sentiment is a CONVICTION MODIFIER — it can raise/lower confidence or push
       toward abstain, but it does NOT pick a different sector than the agents.
  VIX / regime can force the SPY (neutral) or BIL (cash) ABSTAIN actions.
  Politics is RESEARCH-ONLY context, attached to the briefing, never an input here.
  A GOVERNANCE layer holds hard rules the system cannot override.

This file is pure logic — no network, no I/O — so it's easy to test and easy for a
reader to audit. The orchestrator feeds it already-collected inputs.
"""

from dataclasses import dataclass, field, asdict
from datetime import date as _date


# ---- governance: hard rules nothing can override ----------------------
@dataclass
class Governance:
    max_position_pct: float = 30.0       # no single sector > 30%
    halt_vix: float = 35.0               # above this -> force defensive (BIL)
    neutral_vix: float = 25.0            # above this -> bias toward SPY abstain
    min_ensemble_agreement: int = 2      # need >=2 of 3 agents to agree to act
    paper_mode: bool = True              # never auto-executes; human approves


# ---- inputs the orchestrator assembles --------------------------------
@dataclass
class MarketState:
    date: str
    vix: float
    regime: str                          # CALM / NORMAL / STRESSED
    rl_votes: dict                       # {"PPO":"BUY XLF", "A2C":"BUY XLF", "SAC":"HOLD"}
    rl_target: str                       # ensemble target ticker, e.g. "XLF"
    rl_action: str                       # ensemble action, e.g. "BUY"
    rl_confidence: float                 # 0..100 from the ensemble
    current_weight: float = 0.0
    rsi: float = None
    rel_strength: float = None
    news_by_sector: dict = field(default_factory=dict)   # {"XLF": 0.42, ...}  REAL signal
    news_headline: str = None
    political_note: str = None           # RESEARCH-ONLY string or None
    ghost_alpha: float = None
    repo_corroboration: dict = None      # from repo_signals.collect_all(): cross-repo agreement


def _ensemble_agreement(rl_votes: dict, target: str):
    """How many agents voted to act on the target ticker."""
    n = 0
    for v in rl_votes.values():
        if v and target and target in str(v).upper() and "HOLD" not in str(v).upper():
            n += 1
    return n


def decide(state: MarketState, gov: Governance = None) -> dict:
    """
    Run the full decision pipeline. Returns a briefing-ready dict, including the
    final recommended action and a transparent trace of WHY (for the WHY command
    and the research journal).
    """
    gov = gov or Governance()
    trace = []
    confidence = float(state.rl_confidence)
    action = state.rl_action
    ticker = state.rl_target
    abstain_reason = None

    # 1) GOVERNANCE: crisis halt -> force cash (BIL)
    if state.vix >= gov.halt_vix:
        action, ticker = "BUY", "BIL"
        abstain_reason = f"VIX {state.vix} ≥ {gov.halt_vix} crisis halt → defensive cash (BIL)"
        trace.append(abstain_reason)
        confidence = 95.0
        return _briefing(state, action, ticker, confidence, abstain_reason, trace, gov)

    # 2) GOVERNANCE: ensemble must agree, else abstain to SPY (market neutral)
    agreement = _ensemble_agreement(state.rl_votes, state.rl_target)
    if agreement < gov.min_ensemble_agreement:
        action, ticker = "BUY", "SPY"
        abstain_reason = (f"Only {agreement}/3 agents agreed (need {gov.min_ensemble_agreement}) "
                          f"→ abstain to broad market (SPY)")
        trace.append(abstain_reason)
        confidence = min(confidence, 50.0)
        return _briefing(state, action, ticker, confidence, abstain_reason, trace, gov)

    # 3) NEWS as conviction modifier (REAL signal, but cannot change the ticker)
    news = state.news_by_sector.get(state.rl_target)
    if news is not None:
        trace.append(f"News sentiment for {state.rl_target}: {news:+.2f}")
        if state.rl_action == "BUY":
            if news <= -0.30:
                # strong contradicting news -> abstain to SPY
                action, ticker = "BUY", "SPY"
                abstain_reason = (f"RL wanted BUY {state.rl_target} but news is strongly "
                                  f"negative ({news:+.2f}) → abstain to SPY")
                confidence = min(confidence, 45.0)
                trace.append(abstain_reason)
                return _briefing(state, action, ticker, confidence, abstain_reason, trace, gov)
            elif news >= 0.30:
                confidence = min(100.0, confidence + 8)   # confirming news -> small boost
                trace.append("Confirming positive news → confidence +8")
            else:
                trace.append("News neutral → no confidence change")

    # 4) Soft VIX bias: in elevated-but-not-crisis vol, trim confidence
    if state.vix >= gov.neutral_vix:
        confidence = max(0.0, confidence - 10)
        trace.append(f"VIX {state.vix} ≥ {gov.neutral_vix} → confidence −10 (caution)")

    # 5) RSI sanity overlay (note only, doesn't override)
    if state.rsi is not None:
        if state.rsi >= 70 and action == "BUY":
            trace.append(f"⚠️ RSI {state.rsi} overbought — entry may be late")
        elif state.rsi <= 30 and action == "BUY":
            trace.append(f"RSI {state.rsi} oversold — entry timing favorable")

    # 6) MULTI-REPO CORROBORATION (equity-analyzer + algo-system + fed context)
    #    The other repos vote on whether they AGREE with the RL pick. This is the
    #    "all repos feed one decision" layer. It modifies conviction; it never
    #    changes the ticker (RL stays the brain).
    corr = state.repo_corroboration
    if corr and corr.get("total", 0) > 0:
        agree, total = corr["agree"], corr["total"]
        for note in corr.get("notes", []):
            trace.append(f"repo: {note}")
        ratio = agree / total
        if ratio >= 0.66:
            confidence = min(100.0, confidence + 7)
            trace.append(f"Cross-repo agreement {agree}/{total} → confidence +7")
        elif ratio == 0:
            # no repo agrees with the RL pick -> strong caution, abstain to SPY
            action, ticker = "BUY", "SPY"
            abstain_reason = (f"0/{total} other repos corroborated RL's {state.rl_target} pick "
                              f"→ abstain to SPY (no cross-strategy support)")
            confidence = min(confidence, 45.0)
            trace.append(abstain_reason)
            return _briefing(state, action, ticker, round(confidence), abstain_reason, trace, gov)
        else:
            confidence = max(0.0, confidence - 5)
            trace.append(f"Mixed cross-repo support {agree}/{total} → confidence −5")

    return _briefing(state, action, ticker, round(confidence), abstain_reason, trace, gov)


def _briefing(state, action, ticker, confidence, abstain_reason, trace, gov):
    """Assemble the dict telegram_bot.format_briefing() expects, plus the WHY trace."""
    news_val = state.news_by_sector.get(ticker)
    if news_val is None:
        news_val = state.news_by_sector.get(state.rl_target)
    return {
        "date": state.date,
        "regime": state.regime,
        "vix": state.vix,
        "ticker": ticker,
        "action": action,
        "confidence": int(round(confidence)),
        "current_weight": state.current_weight,
        "rl_votes": state.rl_votes,
        "abstain_reason": abstain_reason,
        "news_sentiment": news_val,
        "news_headline": state.news_headline,
        "rsi": state.rsi,
        "rel_strength": state.rel_strength,
        "political_note": state.political_note,   # research-only, just displayed
        "ghost_alpha": state.ghost_alpha,
        "why_trace": trace,                        # consumed by WHY command + journal
        "paper_mode": gov.paper_mode,
    }


if __name__ == "__main__":
    import json

    print("=== Case 1: clean BUY, agents agree, confirming news ===")
    s1 = MarketState(
        date="2026-05-25", vix=18.0, regime="NORMAL",
        rl_votes={"PPO": "BUY XLF", "A2C": "BUY XLF", "SAC": "BUY XLF"},
        rl_target="XLF", rl_action="BUY", rl_confidence=80, current_weight=10,
        rsi=34, rel_strength=1.2, news_by_sector={"XLF": 0.42},
        news_headline="Bank earnings beat", political_note="2 disclosed buys (research only)",
        ghost_alpha=1.8,
    )
    print(json.dumps(decide(s1), indent=2))

    print("\n=== Case 2: agents disagree -> abstain to SPY ===")
    s2 = MarketState(
        date="2026-05-25", vix=18.0, regime="NORMAL",
        rl_votes={"PPO": "BUY XLF", "A2C": "HOLD", "SAC": "HOLD"},
        rl_target="XLF", rl_action="BUY", rl_confidence=70,
        news_by_sector={"XLF": 0.2},
    )
    print(json.dumps(decide(s2), indent=2))

    print("\n=== Case 3: VIX crisis -> force BIL cash ===")
    s3 = MarketState(
        date="2026-05-25", vix=38.0, regime="STRESSED",
        rl_votes={"PPO": "BUY XLK", "A2C": "BUY XLK", "SAC": "BUY XLK"},
        rl_target="XLK", rl_action="BUY", rl_confidence=85,
    )
    print(json.dumps(decide(s3), indent=2))

    print("\n=== Case 4: RL wants BUY but news strongly negative -> abstain to SPY ===")
    s4 = MarketState(
        date="2026-05-25", vix=16.0, regime="CALM",
        rl_votes={"PPO": "BUY XLE", "A2C": "BUY XLE", "SAC": "BUY XLE"},
        rl_target="XLE", rl_action="BUY", rl_confidence=75,
        news_by_sector={"XLE": -0.55}, news_headline="Oil crashes on demand collapse",
    )
    print(json.dumps(decide(s4), indent=2))
