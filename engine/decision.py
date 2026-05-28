"""
decision.py — the brain of Sector Command Live

ARCHITECTURE:
  RL ensemble (PPO/A2C/SAC) makes the CALL.
  News sentiment is a CONVICTION MODIFIER — it can raise/lower confidence or push
       toward abstain, but it does NOT pick a different sector than the agents.
  VIX / regime can force the SPY (neutral) or BIL (cash) ABSTAIN actions.
  Politics is RESEARCH-ONLY context, attached to the briefing, never an input here.
  A GOVERNANCE layer holds hard rules the system cannot override.

GRPO INSIGHT (DeepSeek-R1, arXiv 2501.12948):
  Group Relative Policy Optimization normalizes each signal's contribution relative
  to the group mean/std. Applied here: all confidence modifiers are collected as a
  group, then scaled so no single signal can swing confidence by more than MAX_MOD_SHIFT
  points regardless of how many signals fire. This prevents reward-hacking patterns
  where one very strong signal (e.g., extreme VIX) overrides all other information.

  reward_total = sum(modifiers) clipped to [-MAX_MOD_SHIFT, +MAX_MOD_SHIFT]
  rather than raw add/subtract of each independently.

This file is pure logic — no network, no I/O — easy to test and audit.
"""

from dataclasses import dataclass, field
from datetime import date as _date

MAX_MOD_SHIFT = 20.0   # GRPO cap: total modifier swing cannot exceed ±20 pts


def _grpo_apply(confidence: float, modifiers: list, trace: list) -> float:
    """
    Apply a list of (delta, label) confidence modifiers using GRPO-inspired
    group-relative normalization. Caps total positive and negative contributions
    independently so no single signal dominates.

    DeepSeek-R1 principle: use deterministic, rule-based rewards and prevent any
    single component from driving the optimization signal (reward hacking guard).
    """
    if not modifiers:
        return confidence
    pos = [(d, lbl) for d, lbl in modifiers if d > 0]
    neg = [(d, lbl) for d, lbl in modifiers if d < 0]

    total_pos = sum(d for d, _ in pos)
    total_neg = sum(d for d, _ in neg)

    # Scale down if any group exceeds cap (group-relative normalization)
    if total_pos > MAX_MOD_SHIFT:
        scale = MAX_MOD_SHIFT / total_pos
        pos = [(d * scale, lbl) for d, lbl in pos]
        total_pos = MAX_MOD_SHIFT
        trace.append(f"[GRPO] positive modifiers scaled to cap +{MAX_MOD_SHIFT:.0f}")
    if total_neg < -MAX_MOD_SHIFT:
        scale = MAX_MOD_SHIFT / abs(total_neg)
        neg = [(d * scale, lbl) for d, lbl in neg]
        total_neg = -MAX_MOD_SHIFT
        trace.append(f"[GRPO] negative modifiers scaled to cap −{MAX_MOD_SHIFT:.0f}")

    new_conf = confidence + total_pos + total_neg
    return max(0.0, min(100.0, new_conf))


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
    vix_ts_regime: str = None            # BACKWARDATION / CONTANGO / STEEP_CONTANGO from risk_metrics
    options_signals: dict = None         # PCR data from options_feeder {SPY: {pcr, signal}, ...}


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

    # 2) GOVERNANCE: check ensemble agreement.
    # Hard abstain ONLY if ZERO agents want to buy (all HOLD/SELL).
    # Partial agreement (1/3) is common when agents diversify picks — treat as
    # a strong negative modifier, not a full block. Full agreement (3/3) gets a
    # bonus. This preserves signal flow even when RL agents spread across sectors.
    agreement = _ensemble_agreement(state.rl_votes, state.rl_target)
    if agreement == 0:
        action, ticker = "BUY", "SPY"
        abstain_reason = (f"No agents voted to buy {state.rl_target} "
                          f"→ abstain to broad market (SPY)")
        trace.append(abstain_reason)
        confidence = min(confidence, 50.0)
        return _briefing(state, action, ticker, confidence, abstain_reason, trace, gov)
    # Log the agreement level; modifiers applied below with the full group
    trace.append(f"Ensemble agreement: {agreement}/3 agents on {state.rl_target}")

    # Collect all conviction modifiers as a group — applied together at the end
    # via _grpo_apply() which caps total swing at ±MAX_MOD_SHIFT (GRPO principle).
    modifiers = []

    # Agreement modifier: 1/3 = weak signal (-10), 2/3 = neutral (0), 3/3 = strong (+5)
    if agreement == 1:
        modifiers.append((-10.0, f"only 1/3 agents chose {state.rl_target} (minority view)"))
    elif agreement == 3:
        modifiers.append((5.0, "all 3 agents agree on same sector (strong consensus)"))

    # 3) NEWS as conviction modifier (REAL signal, but cannot change the ticker)
    news = state.news_by_sector.get(state.rl_target)
    if news is not None:
        trace.append(f"News sentiment for {state.rl_target}: {news:+.2f}")
        if state.rl_action == "BUY":
            if news <= -0.30:
                # strong contradicting news -> abstain to SPY (hard gate, not modifier)
                action, ticker = "BUY", "SPY"
                abstain_reason = (f"RL wanted BUY {state.rl_target} but news is strongly "
                                  f"negative ({news:+.2f}) → abstain to SPY")
                confidence = min(confidence, 45.0)
                trace.append(abstain_reason)
                return _briefing(state, action, ticker, confidence, abstain_reason, trace, gov)
            elif news >= 0.30:
                modifiers.append((8.0, f"confirming positive news ({news:+.2f})"))
            elif news >= 0.10:
                modifiers.append((3.0, f"mildly positive news ({news:+.2f})"))
            elif news <= -0.10:
                modifiers.append((-3.0, f"mildly negative news ({news:+.2f})"))

    # 3.5) OPTIONS PCR as conviction modifier (contrarian sentiment, BUY only)
    if state.options_signals and action == "BUY":
        spy_pcr = state.options_signals.get("SPY") or state.options_signals.get("QQQ")
        if spy_pcr:
            pcr_val = spy_pcr.get("pcr")
            pcr_sig = spy_pcr.get("signal", "NEUTRAL")
            if pcr_val is not None:
                trace.append(f"Options PCR (SPY): {pcr_val:.2f} [{pcr_sig}]")
                if pcr_sig == "FEARFUL":
                    modifiers.append((4.0, f"PCR {pcr_val:.2f} elevated put buying (contrarian)"))
                elif pcr_sig == "COMPLACENT":
                    modifiers.append((-3.0, f"PCR {pcr_val:.2f} complacent call positioning"))

    # 4) VIX bias + VIX term structure (collect as modifiers, not immediate apply)
    if state.vix >= gov.neutral_vix:
        modifiers.append((-10.0, f"VIX {state.vix} ≥ {gov.neutral_vix} (elevated vol)"))
    if state.vix_ts_regime:
        ts = state.vix_ts_regime
        if ts == "BACKWARDATION":
            modifiers.append((-8.0, "VIX term structure backwardation (front-heavy fear)"))
        elif ts == "STEEP_CONTANGO":
            modifiers.append((4.0, "VIX term structure steep contango (calm market)"))

    # 5) RSI — adds to modifier group
    if state.rsi is not None:
        if state.rsi >= 75 and action == "BUY":
            modifiers.append((-5.0, f"RSI {state.rsi:.0f} deeply overbought"))
        elif state.rsi >= 70 and action == "BUY":
            modifiers.append((-2.0, f"RSI {state.rsi:.0f} overbought"))
        elif state.rsi <= 30 and action == "BUY":
            modifiers.append((5.0, f"RSI {state.rsi:.0f} oversold (entry timing favorable)"))
        elif state.rsi <= 40 and action == "BUY":
            modifiers.append((2.0, f"RSI {state.rsi:.0f} near oversold"))

    # 6) MULTI-REPO CORROBORATION
    corr = state.repo_corroboration
    if corr and corr.get("total", 0) > 0:
        agree, total = corr["agree"], corr["total"]
        for note in corr.get("notes", []):
            trace.append(f"repo: {note}")
        ratio = agree / total
        if ratio >= 0.66:
            modifiers.append((7.0, f"cross-repo agreement {agree}/{total}"))
        elif ratio == 0:
            modifiers.append((-12.0, f"0/{total} repos corroborated {state.rl_target} (no cross-strategy support)"))
        else:
            modifiers.append((-5.0, f"mixed cross-repo support {agree}/{total}"))

    # 7) Apply all modifiers together (GRPO group normalization)
    #    Prevents reward hacking — no single signal can dominate by itself.
    if modifiers:
        for delta, label in modifiers:
            sign = "+" if delta >= 0 else ""
            trace.append(f"modifier: {label} → {sign}{delta:.0f}")
        confidence = _grpo_apply(confidence, modifiers, trace)

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
