"""Small, dependency-free statistics used by the report service.

Only what the report actually needs: the Student-t distribution (continued
fraction incomplete beta), paired mean differences, worst-case bounds, and
Rubin's rules for combining multiple imputations.
"""

from __future__ import annotations

import math
from typing import Sequence


def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for the incomplete beta function (Lentz's method)."""
    MAXIT, EPS, TINY = 300, 3e-14, 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < TINY:
        d = TINY
    d = 1.0 / d
    h = d
    for m in range(1, MAXIT + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < TINY:
            d = TINY
        c = 1.0 + aa / c
        if abs(c) < TINY:
            c = TINY
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < TINY:
            d = TINY
        c = 1.0 + aa / c
        if abs(c) < TINY:
            c = TINY
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < EPS:
            break
    return h


def reg_incomplete_beta(a: float, b: float, x: float) -> float:
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    log_front = (
        math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
        + a * math.log(x) + b * math.log1p(-x)
    )
    front = math.exp(log_front)
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def t_cdf(t: float, df: float) -> float:
    if df <= 0:
        raise ValueError("df must be positive")
    if math.isinf(t):
        return 1.0 if t > 0 else 0.0
    x = df / (df + t * t)
    tail = 0.5 * reg_incomplete_beta(df / 2.0, 0.5, x)
    return 1.0 - tail if t > 0 else tail


def t_quantile(p: float, df: float) -> float:
    """Bisection inversion; monotone, no external tables."""
    if not (0.0 < p < 1.0):
        raise ValueError("p must be in (0, 1)")
    if p == 0.5:
        return 0.0
    lo, hi = (0.0, 1e4) if p > 0.5 else (-1e4, 0.0)
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if t_cdf(mid, df) < p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def t_p_value(t: float, df: float) -> float:
    return max(0.0, min(1.0, 2.0 * (1.0 - t_cdf(abs(t), df))))


def mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def sample_sd(values: Sequence[float]) -> float:
    n = len(values)
    if n < 2:
        return float("nan")
    mu = mean(values)
    return math.sqrt(sum((v - mu) ** 2 for v in values) / (n - 1))


def paired_summary(diffs: Sequence[float], alpha: float = 0.05) -> dict:
    n = len(diffs)
    if n < 2:
        return {
            "n": n,
            "mean_difference": mean(diffs) if n else None,
            "sd": None,
            "se": None,
            "df": None,
            "t": None,
            "p_value": None,
            "ci_low": None,
            "ci_high": None,
            "ci_level": 1 - alpha,
            "note": "fewer than two complete pairs; interval not defined",
        }
    mu = mean(diffs)
    sd = sample_sd(diffs)
    se = sd / math.sqrt(n)
    df = n - 1
    if se == 0.0:
        return {
            "n": n,
            "mean_difference": mu,
            "sd": sd,
            "se": 0.0,
            "df": df,
            "t": None,
            "p_value": None,
            "ci_low": mu,
            "ci_high": mu,
            "ci_level": 1 - alpha,
            "note": "zero variability; interval collapses to the point estimate",
        }
    t_stat = mu / se
    crit = t_quantile(1 - alpha / 2, df)
    return {
        "n": n,
        "mean_difference": mu,
        "sd": sd,
        "se": se,
        "df": df,
        "t": t_stat,
        "p_value": t_p_value(t_stat, df),
        "ci_low": mu - crit * se,
        "ci_high": mu + crit * se,
        "ci_level": 1 - alpha,
        "critical_value": crit,
    }


def rubin_combine(estimates: Sequence[float], variances: Sequence[float], alpha: float = 0.05) -> dict:
    m = len(estimates)
    if m < 2:
        raise ValueError("multiple imputation needs at least two completed imputations")
    qbar = sum(estimates) / m
    ubar = sum(variances) / m
    between = sum((q - qbar) ** 2 for q in estimates) / (m - 1)
    total = ubar + (1.0 + 1.0 / m) * between
    se = math.sqrt(total)
    if between > 0:
        df = (m - 1) * (1.0 + ubar / ((1.0 + 1.0 / m) * between)) ** 2
    else:
        df = float("inf")
    crit = t_quantile(1 - alpha / 2, df) if math.isfinite(df) else 1.959963984540054
    p = t_p_value(qbar / se, df) if math.isfinite(df) and se > 0 else (
        0.0 if se == 0 and qbar != 0 else 1.0
    )
    return {
        "m": m,
        "mean_difference": qbar,
        "se": se,
        "within_variance": ubar,
        "between_variance": between,
        "total_variance": total,
        "df": df if math.isfinite(df) else None,
        "ci_low": qbar - crit * se,
        "ci_high": qbar + crit * se,
        "p_value": p,
        "ci_level": 1 - alpha,
        "fraction_missing_information": (
            ((1.0 + 1.0 / m) * between / total) if total > 0 else 0.0
        ),
    }
