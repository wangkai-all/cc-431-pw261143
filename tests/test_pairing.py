from __future__ import annotations

import copy
import random

import pytest

from app.pairing.engine import PairingError, build_plan
from app.pairing.policy import MatchingPolicy, policy_from_dict


def candidate(obs_id, arm, site="north", band="mild", baseline=50.0, **extra):
    return {
        "obs_id": obs_id,
        "arm": arm,
        "site": site,
        "severity_band": band,
        "baseline_rom": baseline,
        **extra,
    }


def ls(rows):
    return [candidate(f"{arm[0]}{i}", arm, **rest) for i, (arm, rest) in enumerate(rows)]


def test_pairs_are_deterministic_and_order_independent():
    rows = [
        candidate(f"i{i}", "intervention", baseline=40 + i) for i in range(5)
    ] + [candidate(f"c{i}", "control", baseline=41 + i) for i in range(5)]
    policy = MatchingPolicy(caliper=20.0)
    first = build_plan(rows, policy)
    shuffled = copy.deepcopy(rows)
    random.Random(7).shuffle(shuffled)
    second = build_plan(shuffled, policy)
    assert first["pairs"] == second["pairs"]
    assert first["exclusions"] == second["exclusions"]


def test_caliper_exclusions_are_listed_with_basis():
    rows = [
        candidate("t1", "intervention", baseline=10.0),
        candidate("c1", "control", baseline=60.0),
    ]
    plan = build_plan(rows, MatchingPolicy(caliper=5.0))
    assert plan["pairs"] == []
    reasons = {(e["obs_id"], e["reason"]) for e in plan["exclusions"]}
    assert ("t1", "caliper_exceeded") in reasons
    assert ("c1", "not_selected") in reasons
    caliper_row = next(e for e in plan["exclusions"] if e["obs_id"] == "t1")
    assert caliper_row["nearest_distance"] == pytest.approx(50.0)


def test_missing_is_never_coerced_to_a_match():
    rows = [
        candidate("t1", "intervention", baseline=None),
        candidate("c1", "control", baseline=50.0),
    ]
    with pytest.raises(PairingError):
        build_plan(rows, MatchingPolicy())


def test_outcome_like_fields_are_rejected_by_the_matcher():
    rows = [
        candidate("t1", "intervention", outcome_rom=61.0),
        candidate("c1", "control"),
    ]
    with pytest.raises(PairingError, match="covariate-only"):
        build_plan(rows, MatchingPolicy())


def test_exact_ties_are_reported_and_seeded():
    rows = [
        candidate("t1", "intervention", baseline=50.0),
        candidate("c1", "control", baseline=49.0),
        candidate("c2", "control", baseline=51.0),
    ]
    plan = build_plan(rows, MatchingPolicy(caliper=5.0, tie_break_seed=3))
    assert len(plan["pairs"]) == 1
    pair = plan["pairs"][0]
    assert pair["tie_broken"] is True
    assert pair["tie_candidates"] == 2
    assert pair["control_obs_id"] in {"c1", "c2"}


def test_policy_validation_rejects_outcome_bearing_fields():
    with pytest.raises(ValueError):
        MatchingPolicy(match_fields=("outcome",))  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        policy_from_dict({"policy": "anything"})
    with pytest.raises(ValueError):
        MatchingPolicy(caliper=0)
    with pytest.raises(ValueError):
        MatchingPolicy(tie_break_seed="seed")  # type: ignore[arg-type]


def test_pairs_are_unique_and_controls_are_not_reused():
    rows = [candidate(f"i{i}", "intervention", baseline=40 + i) for i in range(4)] + [
        candidate(f"c{i}", "control", baseline=40 + i) for i in range(2)
    ]
    plan = build_plan(rows, MatchingPolicy(caliper=30.0))
    controls_used = [p["control_obs_id"] for p in plan["pairs"]]
    assert len(controls_used) == len(set(controls_used)) == 2
    assert plan["summary"]["excluded_by_reason"].get("control_pool_exhausted") == 2
    assert sum(1 for e in plan["exclusions"] if e["reason"] == "control_pool_exhausted") == 2


def test_cells_are_respected():
    rows = [
        candidate("t1", "intervention", site="north", band="mild", baseline=50.0),
        candidate("c1", "control", site="north", band="severe", baseline=50.0),
    ]
    plan = build_plan(rows, MatchingPolicy(caliper=10.0))
    assert plan["pairs"] == []


def test_policy_checksum_changes_with_policy():
    a = MatchingPolicy()
    b = MatchingPolicy(caliper=4.0)
    assert a.checksum() != b.checksum()
    assert MatchingPolicy().checksum() == MatchingPolicy().checksum()


def test_plan_does_not_mutate_candidates():
    rows = [candidate("t1", "intervention"), candidate("c1", "control")]
    snapshot = copy.deepcopy(rows)
    build_plan(rows, MatchingPolicy())
    assert rows == snapshot
