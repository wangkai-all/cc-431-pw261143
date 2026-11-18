from __future__ import annotations

import math

import pytest

from app.stats.statlib import (
    mean,
    paired_summary,
    reg_incomplete_beta,
    rubin_combine,
    sample_sd,
    t_cdf,
    t_p_value,
    t_quantile,
)


@pytest.mark.parametrize(
    "df,p,expected",
    [
        (1, 0.975, 12.7062047362),
        (10, 0.975, 2.2281388520),
        (41, 0.975, 2.0195409704),
        (100, 0.995, 2.6258905180),
        (41, 0.95, 1.6828780864),
    ],
)
def test_t_quantile_matches_reference_tables(df, p, expected):
    assert t_quantile(p, df) == pytest.approx(expected, abs=5e-6)


def test_t_cdf_symmetry_and_known_values():
    assert t_cdf(0.0, 7) == pytest.approx(0.5)
    assert t_cdf(-2.0, 30) == pytest.approx(1 - t_cdf(2.0, 30))
    assert t_cdf(2.0, 30) == pytest.approx(0.972687, abs=1e-6)


def test_p_value_round_trip():
    for df in (1, 5, 41, 200):
        for t in (0.5, 1.96, 3.0):
            crit = t_quantile(1 - t_p_value(t, df) / 2, df)
            assert crit == pytest.approx(abs(t), abs=1e-5)


def test_incomplete_beta_edges():
    assert reg_incomplete_beta(2, 3, 0.0) == 0.0
    assert reg_incomplete_beta(2, 3, 1.0) == 1.0
    assert reg_incomplete_beta(2, 3, 0.5) == pytest.approx(0.6875, abs=1e-9)


def test_paired_summary_against_hand_calculation():
    diffs = [2.0, 4.0, 6.0]
    out = paired_summary(diffs)
    assert out["n"] == 3
    assert out["mean_difference"] == pytest.approx(4.0)
    assert out["sd"] == pytest.approx(2.0)
    assert out["se"] == pytest.approx(2.0 / math.sqrt(3))
    assert out["df"] == 2
    crit = t_quantile(0.975, 2)
    assert out["ci_low"] == pytest.approx(4.0 - crit * 2.0 / math.sqrt(3))
    assert out["ci_high"] == pytest.approx(4.0 + crit * 2.0 / math.sqrt(3))


def test_paired_summary_single_pair_has_no_interval():
    out = paired_summary([1.0])
    assert out["n"] == 1
    assert out["ci_low"] is None
    assert "interval not defined" in out["note"]


def test_paired_summary_zero_variance_is_flagged():
    out = paired_summary([2.0, 2.0, 2.0])
    assert out["ci_low"] == out["ci_high"] == 2.0
    assert "zero variability" in out["note"]


def test_rubin_combine_reduces_to_within_variance_without_between_spread():
    out = rubin_combine([1.0, 1.0, 1.0], [0.25, 0.25, 0.25])
    assert out["mean_difference"] == pytest.approx(1.0)
    assert out["between_variance"] == 0.0
    assert out["se"] == pytest.approx(0.5)
    assert out["fraction_missing_information"] == 0.0
    assert out["df"] is None


def test_rubin_combine_penalises_between_imputation_spread():
    out = rubin_combine([2.0, 4.0], [1.0, 1.0])
    assert out["mean_difference"] == pytest.approx(3.0)
    assert out["between_variance"] == pytest.approx(2.0)
    # total = 1.0 + 1.5*2.0
    assert out["total_variance"] == pytest.approx(4.0)
    # df = (m-1) * (1 + ubar / ((1 + 1/m) * between)) ** 2
    assert out["df"] == pytest.approx((1 + 1.0 / 3.0) ** 2)
    assert out["fraction_missing_information"] == pytest.approx(0.75)


def test_mean_and_sd():
    assert mean([1, 2, 3]) == 2.0
    assert sample_sd([2, 4, 4, 4, 5, 5, 7, 9]) == pytest.approx(2.13809, abs=1e-4)
