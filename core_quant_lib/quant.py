"""
core_quant_lib — shared quantitative functions

The consolidation move: math that currently lives duplicated across your repos
(Black-Scholes, Monte Carlo, regime detection) belongs in ONE place. Over time,
move the canonical implementation here and have each repo import it:

    pip install git+https://github.com/cameroncc333/core-quant-lib

This file is a starter with the two functions that recur most. Migrate the rest
incrementally — don't rip everything out at once.
"""

import math


def black_scholes(S, K, T, r, sigma, option="call"):
    """
    European option price + Greeks. Same partial-derivative framework used in the
    AAS pricing model (∂Cost/∂φ) and the equity analyzer's options tab.
    Returns dict with price, delta, gamma, theta, vega, rho.
    """
    if T <= 0 or sigma <= 0:
        intrinsic = max(0.0, (S - K) if option == "call" else (K - S))
        return {"price": intrinsic, "delta": 0, "gamma": 0, "theta": 0, "vega": 0, "rho": 0}

    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    Nd1 = 0.5 * (1 + math.erf(d1 / math.sqrt(2)))
    Nd2 = 0.5 * (1 + math.erf(d2 / math.sqrt(2)))
    nd1 = math.exp(-0.5 * d1 ** 2) / math.sqrt(2 * math.pi)

    if option == "call":
        price = S * Nd1 - K * math.exp(-r * T) * Nd2
        delta = Nd1
        theta = (-S * nd1 * sigma / (2 * math.sqrt(T))
                 - r * K * math.exp(-r * T) * Nd2)
        rho = K * T * math.exp(-r * T) * Nd2
    else:  # put
        price = K * math.exp(-r * T) * (1 - Nd2) - S * (1 - Nd1)
        delta = Nd1 - 1
        theta = (-S * nd1 * sigma / (2 * math.sqrt(T))
                 + r * K * math.exp(-r * T) * (1 - Nd2))
        rho = -K * T * math.exp(-r * T) * (1 - Nd2)

    gamma = nd1 / (S * sigma * math.sqrt(T))
    vega = S * nd1 * math.sqrt(T)
    return {"price": round(price, 4), "delta": round(delta, 4),
            "gamma": round(gamma, 6), "theta": round(theta / 365, 4),
            "vega": round(vega / 100, 4), "rho": round(rho / 100, 4)}


def detect_regime(realized_vol_20d, vix):
    """
    Blended regime classifier shared by the RL env and the live engine.
    blended = 0.6 * realized_vol + 0.4 * (VIX/100)
    """
    blended = 0.6 * realized_vol_20d + 0.4 * (vix / 100.0)
    if blended < 0.10:
        return "CALM", 1.0
    elif blended < 0.20:
        return "NORMAL", 1.2
    return "STRESSED", 1.5


if __name__ == "__main__":
    print("BS call:", black_scholes(100, 100, 0.5, 0.045, 0.2, "call"))
    print("Regime:", detect_regime(0.12, 24.5))
