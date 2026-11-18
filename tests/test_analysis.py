from __future__ import annotations

import copy
import math

import pytest

from app.stats.analysis import assemble, build_report

OUTCOME_SPEC = {
    "label": "ROM",
    "unit": "degrees",
    "min": 0.0,
    "max": 90.0,
    "higher_is_better": True,
    "primary": True,
}


def pair(pid, treated, control, cell=None, distance=1.0):
    return {
        "pair_id": pid,
        "cell": cell or {"site": "north", "severity_band": "mild"},
        "treated_obs_id": f"t-{pid}",
        "control_obs_id": f"c-{pid}",
        "treated_value": 50.0,
        "control_value": 50.0,
        "distance": distance,
        "tie_broken": False,
        "tie_candidates": 1,
    }


def outcomes(values):
    return {
        oid: {
            "rom_week12": (
                {"status": "reported", "value": v, "measurement_id": f"m-{oid}", "revision": 1}
                if v is not None
                else {"status": "absent"}
            )
        }
        for oid, v in values.items()
    }


def test_missing_outcome_excludes_pair_and_never_fills_zero():
    pairs = [pair("001", 60.0, 55.0), pair("002", 60.0, 55.0)]
    rows = outcomes({"t-001": 60.0, "c-001": 55.0, "t-002": None, "c-002": 55.0})
    result = assemble(pairs, rows, "rom_week12", OUTCOME_SPEC, higher_is_better=True)
    assert result["counts"] == {"pairs_in_plan": 2, "complete_pairs": 1, "excluded_pairs": 1}
    assert [p["pair_id"] for p in result["complete"]] == ["001"]
    missing = result["missing"][0]
    assert missing["pair_id"] == "002"
    assert missing["missing_sides"] == [{"obs_id": "t-002", "arm": "intervention"}]


def test_withdrawn_outcome_counts_as_missing():
    pairs = [pair("001", 60.0, 55.0)]
    rows = {
        "t-001": {"rom_week12": {"status": "withdrawn", "value": None}},
        "c-001": {"rom_week12": {"status": "reported", "value": 55.0}},
    }
    result = assemble(pairs, rows, "rom_week12", OUTCOME_SPEC, higher_is_better=True)
    assert result["counts"]["complete_pairs"] == 0
    assert result["missing"][0]["reason"] == "outcome_not_reported"


def test_favourable_direction_uses_declared_orientation():
    pairs = [pair("001", 3.0, 6.0)]
    rows = outcomes({"t-001": 3.0, "c-001": 6.0})
    lower_better = {**OUTCOME_SPEC, "higher_is_better": False}
    lowered = assemble(pairs, rows, "rom_week12", lower_better, higher_is_better=False)
    assert lowered["complete"][0]["favourable_difference"] == 3.0
    assert lowered["complete"][0]["raw_difference"] == -3.0


def make_report(diffs, missing_pairs=0, bounds=None):
    pairs, values = [], {}
    for i, d in enumerate(diffs):
        pid = f"{i:03d}"
        pairs.append(pair(pid, 50.0 + d, 50.0))
        values[f"t-{pid}"] = 50.0 + d
        values[f"c-{pid}"] = 50.0
    for j in range(missing_pairs):
        pid = f"m{j:03d}"
        pairs.append(pair(pid, 50.0, 50.0))
        values[f"t-{pid}"] = None
        values[f"c-{pid}"] = 50.0
    plan = {
        "plan_version": 1,
        "plan_version_id": "plan_test",
        "plan_checksum": "abc",
        "policy": {"match_fields": ["site"]},
        "policy_checksum": "def",
        "observation_fingerprint": "ghi",
        "summary": {"paired": len(pairs)},
        "pairs": pairs,
        "exclusions": [],
    }
    payload = {
        "version": 1,
        "outcome_definitions": {"rom_week12": OUTCOME_SPEC},
        "rows": outcomes(values),
    }
    return build_report(
        outcomes_payload=payload,
        plan=plan,
        plan_text="{}",
        outcome_name="rom_week12",
        alpha=0.05,
        mi_seed=123,
        mi_draws=10,
        higher_is_better=True,
        missing_bounds=bounds,
    )


def test_complete_case_estimate_matches_hand_calculation():
    report = make_report([2.0, 4.0, 6.0])
    cc = report["analysis"]["complete_case"]
    assert cc["mean_difference"] == pytest.approx(4.0)
    assert cc["n"] == 3
    assert report["analysis"]["sensitivity"] == []


def test_sensitivity_is_separate_and_labelled_as_a_bound():
    report = make_report([2.0, 4.0, 6.0], missing_pairs=2)
    cc = report["analysis"]["complete_case"]
    sens = report["analysis"]["sensitivity"]
    assert cc["n"] == 3
    bounded = next(s for s in sens if s["scenario"] == "bounded_worst_best")
    assert bounded["is_estimate"] is False
    assert bounded["imputed_pairs"] == 2
    assert "not a point estimate" in bounded["note"]
    # Bound is computed on the full n and widens past the complete-case result.
    assert bounded["interval_low"] < cc["ci_low"]
    assert bounded["interval_high"] > cc["ci_high"]
    assert report["analysis"]["distinction"]["complete_case"].startswith("Estimate")


def test_declared_bounds_must_lie_in_the_plausible_range():
    with pytest.raises(ValueError):
        make_report([1.0, 2.0], missing_pairs=1, bounds={"low": -5.0, "high": 10.0})


def test_declared_bounds_change_the_bound_not_the_complete_case():
    wide = make_report([2.0, 4.0, 6.0], missing_pairs=2)
    narrow = make_report([2.0, 4.0, 6.0], missing_pairs=2, bounds={"low": 40.0, "high": 60.0})
    assert wide["analysis"]["complete_case"]["mean_difference"] == narrow["analysis"]["complete_case"]["mean_difference"]
    wb = next(s for s in wide["analysis"]["sensitivity"] if s["scenario"] == "bounded_worst_best")
    nb = next(s for s in narrow["analysis"]["sensitivity"] if s["scenario"] == "bounded_worst_best")
    assert nb["interval_high"] < wb["interval_high"]
    assert "Declared bounds: 40 to 60" in nb["detail"]


def test_tipped_to_null_uses_zero_contrast():
    report = make_report([4.0, 4.0], missing_pairs=2)
    tipped = next(s for s in report["analysis"]["sensitivity"] if s["scenario"] == "tipped_to_null")
    assert tipped["mean_difference"] == pytest.approx(2.0)


def test_multiple_imputation_is_seeded_and_reproducible():
    a = make_report([1.0, 2.0, 3.0, 4.0], missing_pairs=2)
    b = make_report([1.0, 2.0, 3.0, 4.0], missing_pairs=2)
    ma = next(s for s in a["analysis"]["sensitivity"] if s["scenario"] == "imputed_mi")
    mb = next(s for s in b["analysis"]["sensitivity"] if s["scenario"] == "imputed_mi")
    assert ma["combined"] == mb["combined"]
    assert ma["seed"] == 123


def test_report_hashes_are_stable_and_sensitive_to_inputs():
    a = make_report([1.0, 2.0, 3.0])
    b = make_report([1.0, 2.0, 3.0])
    c = make_report([1.0, 2.0, 9.0])
    assert a["provenance"]["report_input_hash"] == b["provenance"]["report_input_hash"]
    assert a["provenance"]["report_input_hash"] != c["provenance"]["report_input_hash"]


def test_no_pairs_at_all_is_reported_without_crashing():
    empty_plan = {
        "plan_version": 1,
        "plan_version_id": "p",
        "plan_checksum": "x",
        "policy": {},
        "policy_checksum": "y",
        "observation_fingerprint": "z",
        "summary": {"paired": 0},
        "pairs": [],
        "exclusions": [],
    }
    report = build_report(
        outcomes_payload={"version": 0, "outcome_definitions": {"rom_week12": OUTCOME_SPEC}, "rows": {}},
        plan=empty_plan,
        plan_text="{}",
        outcome_name="rom_week12",
        alpha=0.05,
        mi_seed=1,
        mi_draws=5,
        higher_is_better=True,
    )
    cc = report["analysis"]["complete_case"]
    assert cc["n"] == 0
    assert cc["mean_difference"] is None
    assert cc["ci_low"] is None
