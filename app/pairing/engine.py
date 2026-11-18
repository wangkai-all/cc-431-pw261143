"""Deterministic, covariate-only pairing engine.

Contract
--------
The engine's only input is the covariate table. It has no parameter that could
accept an outcome, which makes "choose the more favourable pairing after seeing
results" structurally impossible rather than merely discouraged.

Determinism is order-independent: cells, subjects and candidate controls are
ordered by ``(balance value, sha256(seed:obs_id))`` so that shuffling the input
or replaying on another machine yields byte-identical pairs. The seed is fixed
in the policy; exact ties are recorded on the pair rather than hidden.
"""

from __future__ import annotations

import hashlib
from typing import Any

from .policy import MatchingPolicy

PAIRABLE_ARMS = ("intervention", "control")
TREATED = "intervention"
CONTROL = "control"


class PairingError(ValueError):
    pass


def _tiebreak(seed: int, obs_id: str) -> str:
    return hashlib.sha256(f"{seed}:{obs_id}".encode("utf-8")).hexdigest()


def _validate_candidates(candidates: list[dict[str, Any]], policy: MatchingPolicy) -> None:
    seen: set[str] = set()
    required = {"obs_id", "arm", policy.balance_field, *policy.match_fields}
    for row in candidates:
        missing = required - set(row)
        if missing:
            raise PairingError(f"candidate {row.get('obs_id')} missing fields {sorted(missing)}")
        if row["obs_id"] in seen:
            raise PairingError(f"duplicate obs_id in candidate table: {row['obs_id']}")
        seen.add(row["obs_id"])
        if row["arm"] not in PAIRABLE_ARMS:
            raise PairingError(f"unknown arm {row['arm']!r} on {row['obs_id']}")
        value = row[policy.balance_field]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise PairingError(f"non-numeric {policy.balance_field} on {row['obs_id']}")
        # Outcome-looking fields must never ride along into the matcher.
        leaked = [k for k in row if k.startswith("outcome") or k in {"value", "result"}]
        if leaked:
            raise PairingError(
                f"candidate table carries outcome-like field(s) {leaked}; the matcher is covariate-only"
            )


def build_plan(candidates: list[dict[str, Any]], policy: MatchingPolicy) -> dict[str, Any]:
    """Return an immutable plan. Never mutates ``candidates``."""
    _validate_candidates(candidates, policy)

    cells: dict[tuple, dict[str, list[dict[str, Any]]]] = {}
    for row in candidates:
        key = tuple(row[f] for f in policy.match_fields)
        bucket = cells.setdefault(key, {TREATED: [], CONTROL: []})
        bucket[row["arm"]].append(row)

    pairs: list[dict[str, Any]] = []
    exclusions: list[dict[str, Any]] = []
    used_controls: set[str] = set()

    for key in sorted(cells):
        bucket = cells[key]
        cell = dict(zip(policy.match_fields, key))

        def sort_key(row: dict[str, Any]) -> tuple[float, str]:
            return (float(row[policy.balance_field]), _tiebreak(policy.tie_break_seed, row["obs_id"]))

        treated_rows = sorted(bucket[TREATED], key=sort_key)
        control_rows = sorted(bucket[CONTROL], key=sort_key)

        for t_row in treated_rows:
            t_val = float(t_row[policy.balance_field])
            available = [c for c in control_rows if c["obs_id"] not in used_controls]
            if not available:
                exclusions.append(
                    {
                        "obs_id": t_row["obs_id"],
                        "arm": TREATED,
                        "cell": cell,
                        "reason": "control_pool_exhausted",
                        "detail": "no unpaired control participant remains in this cell",
                        "balance_value": t_val,
                    }
                )
                continue
            distances = [
                (abs(t_val - float(c[policy.balance_field])), _tiebreak(policy.tie_break_seed, c["obs_id"]), c)
                for c in available
            ]
            best_dist, _, best = min(distances, key=lambda d: (d[0], d[1]))
            tie_count = sum(1 for d in distances if abs(d[0] - best_dist) < 1e-12)
            if best_dist > float(policy.caliper) + 1e-12:
                exclusions.append(
                    {
                        "obs_id": t_row["obs_id"],
                        "arm": TREATED,
                        "cell": cell,
                        "reason": "caliper_exceeded",
                        "detail": (
                            f"nearest eligible control differs by {best_dist:.3f} on "
                            f"{policy.balance_field}, above the pre-declared caliper {policy.caliper:g}"
                        ),
                        "balance_value": t_val,
                        "nearest_distance": round(best_dist, 6),
                        "nearest_obs_id": best["obs_id"],
                    }
                )
                continue
            used_controls.add(best["obs_id"])
            pairs.append(
                {
                    "pair_id": f"pair_{len(pairs) + 1:03d}",
                    "cell": cell,
                    "treated_obs_id": t_row["obs_id"],
                    "control_obs_id": best["obs_id"],
                    "treated_value": t_val,
                    "control_value": float(best[policy.balance_field]),
                    "distance": round(best_dist, 6),
                    "tie_broken": tie_count > 1,
                    "tie_candidates": tie_count,
                }
            )

    for key in sorted(cells):
        for c_row in sorted(cells[key][CONTROL], key=lambda r: r["obs_id"]):
            if c_row["obs_id"] not in used_controls:
                exclusions.append(
                    {
                        "obs_id": c_row["obs_id"],
                        "arm": CONTROL,
                        "cell": dict(zip(policy.match_fields, key)),
                        "reason": "not_selected",
                        "detail": "control remained after nearest-neighbour pass; not part of a pair",
                        "balance_value": float(c_row[policy.balance_field]),
                    }
                )

    exclusions.sort(key=lambda e: (e["obs_id"],))
    pairs.sort(key=lambda p: (p["treated_obs_id"], p["control_obs_id"]))
    # renumber stably after the sort
    for i, pair in enumerate(pairs, start=1):
        pair["pair_id"] = f"pair_{i:03d}"

    treated_total = sum(1 for row in candidates if row["arm"] == TREATED)
    control_total = sum(1 for row in candidates if row["arm"] == CONTROL)
    summary = {
        "treated": treated_total,
        "controls": control_total,
        "paired": len(pairs),
        "excluded": len(exclusions),
        "excluded_by_reason": {
            reason: sum(1 for e in exclusions if e["reason"] == reason)
            for reason in sorted({e["reason"] for e in exclusions})
        },
        "cells": len(cells),
        "ties_broken": sum(1 for p in pairs if p["tie_broken"]),
        "max_distance": max((p["distance"] for p in pairs), default=0.0),
    }
    return {
        "policy": policy.to_dict(),
        "policy_checksum": policy.checksum(),
        "pairs": pairs,
        "exclusions": exclusions,
        "summary": summary,
        "uses_outcomes": False,
    }
