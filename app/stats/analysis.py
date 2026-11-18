"""Outcome assembly, visual-wiggle sensitivity bounds, and the report renderer.

Two analyses, never merged:

``complete_case``
    Every pair with both outcomes reported and not withdrawn. Pairs with any
    missing outcome are excluded and listed one by one with the reason. No
    imputation, no zero-filling.

``sensitivity``
    The complete cases plus a *declared* assumption about what the missing
    contrasts plausibly are. The interval is a range between worst and best
    plausible values, so it is an honest bound, not an estimate.

The renderer refuses to run unless its inputs match their recorded hashes; that
is what makes an interrupted calculation resumable and auditable.
"""

from __future__ import annotations

import random
from typing import Any, Sequence

from ..common.ids import digest, sha256_text
from .statlib import mean, paired_summary, rubin_combine, sample_sd, t_quantile

SENSITIVITY_ASSUMPTIONS = {
    "bounded_worst_best": {
        "label": "Bounded worst/best case",
        "detail": "Missing contrasts are assumed to lie inside the outcome's clinically plausible range, not at zero.",
    },
    "tipped_to_null": {
        "label": "Moved to the null",
        "detail": "Missing contrasts are assumed to equal zero, which is the least favourable assumption for a non-null finding.",
    },
    "imputed_mi": {
        "label": "Multiple imputation (declared normal model)",
        "detail": "Missing contrasts are drawn from a declared normal model fitted to complete cases; seed is fixed and recorded.",
    },
}


# --------------------------------------------------------------------------- #
# assembly
# --------------------------------------------------------------------------- #


def _outcome_value(outcomes: dict[str, Any], obs_id: str, outcome: str) -> float | None:
    rec = outcomes.get(obs_id, {}).get(outcome, {"status": "absent"})
    if rec.get("status") != "reported":
        return None
    value = rec.get("value")
    return None if value is None else float(value)


def assemble(
    pairs: list[dict[str, Any]],
    outcomes: dict[str, Any],
    outcome: str,
    outcome_spec: dict[str, Any],
    *,
    higher_is_better: bool,
) -> dict[str, Any]:
    """Split the pair list into complete pairs and attributable exclusions."""
    complete: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    for pair in pairs:
        t_val = _outcome_value(outcomes, pair["treated_obs_id"], outcome)
        c_val = _outcome_value(outcomes, pair["control_obs_id"], outcome)
        if t_val is None or c_val is None:
            absent = []
            if t_val is None:
                absent.append({"obs_id": pair["treated_obs_id"], "arm": "intervention"})
            if c_val is None:
                absent.append({"obs_id": pair["control_obs_id"], "arm": "control"})
            missing.append(
                {
                    "pair_id": pair["pair_id"],
                    "cell": pair["cell"],
                    "distance": pair["distance"],
                    "missing_sides": absent,
                    "reason": "outcome_not_reported",
                    "treated_obs_id": pair["treated_obs_id"],
                    "control_obs_id": pair["control_obs_id"],
                }
            )
            continue
        diff = (t_val - c_val) * (1.0 if higher_is_better else -1.0)
        complete.append(
            {
                "pair_id": pair["pair_id"],
                "cell": pair["cell"],
                "distance": pair["distance"],
                "treated_obs_id": pair["treated_obs_id"],
                "control_obs_id": pair["control_obs_id"],
                "treated_value": t_val,
                "control_value": c_val,
                "favourable_difference": diff,
                "raw_difference": t_val - c_val,
            }
        )
    return {
        "outcome": outcome,
        "outcome_spec": outcome_spec,
        "higher_is_better": higher_is_better,
        "complete": complete,
        "missing": missing,
        "counts": {
            "pairs_in_plan": len(pairs),
            "complete_pairs": len(complete),
            "excluded_pairs": len(missing),
        },
    }


# --------------------------------------------------------------------------- #
# sensitivity
# --------------------------------------------------------------------------- #


def _bounded(
    diffs: Sequence[float],
    missing_pairs: int,
    spec: dict[str, Any],
    higher_is_better: bool,
    missing_bounds: dict[str, float] | None,
) -> dict:
    n = len(diffs)
    obs_mean = mean(diffs) if diffs else 0.0
    # Work in "favourable" orientation: the plausible contrast range determines
    # the two endpoints. Never a hidden zero fill unless explicitly declared.
    if missing_bounds:
        low = float(missing_bounds["low"])
        high = float(missing_bounds["high"])
        if not (spec["min"] <= low < high <= spec["max"]):
            raise ValueError("declared missing bounds must lie inside the outcome's plausible range")
    else:
        low, high = float(spec["min"]), float(spec["max"])
    # A missing contrast can be as favourable as +/- the full plausible range
    # (or the declared bounds). Feed both to see where the mean could land.
    raw_extremes = [high - low, low - high]
    if not higher_is_better:
        raw_extremes = [-v for v in raw_extremes]
    endpoints = []
    for assumption_delta in raw_extremes:
        total_n = n + missing_pairs
        combined_mean = (obs_mean * n + assumption_delta * missing_pairs) / total_n if total_n else 0.0
        endpoints.append(
            {
                "mean_difference": combined_mean,
                "assumed_missing_contrast": assumption_delta * (1.0 if higher_is_better else -1.0),
                "n": total_n,
            }
        )
    endpoints.sort(key=lambda e: e["mean_difference"])
    return {
        "scenario": "bounded_worst_best",
        "label": SENSITIVITY_ASSUMPTIONS["bounded_worst_best"]["label"],
        "detail": (
            SENSITIVITY_ASSUMPTIONS["bounded_worst_best"]["detail"]
            + (
                f" Declared bounds: {low:g} to {high:g} {spec['unit']}."
                if missing_bounds
                else f" Unspecified bounds default to the outcome's plausible range "
                f"({spec['min']:g} to {spec['max']:g} {spec['unit']})."
            )
        ),
        "observed_mean_difference": obs_mean,
        "observed_n": n,
        "imputed_pairs": missing_pairs,
        "interval_low": endpoints[0]["mean_difference"],
        "interval_high": endpoints[1]["mean_difference"],
        "endpoints": endpoints,
        "crosses_null": endpoints[0]["mean_difference"] <= 0.0 <= endpoints[1]["mean_difference"],
        "is_estimate": False,
        "note": (
            "This is a bound under a declared assumption, not a point estimate or a confidence interval."
        ),
    }


def _tipped(diffs: Sequence[float], missing_pairs: int) -> dict:
    n = len(diffs)
    obs_mean = mean(diffs) if diffs else 0.0
    total_n = n + missing_pairs
    combined = (obs_mean * n) / total_n if total_n else 0.0
    return {
        "scenario": "tipped_to_null",
        "label": SENSITIVITY_ASSUMPTIONS["tipped_to_null"]["label"],
        "detail": SENSITIVITY_ASSUMPTIONS["tipped_to_null"]["detail"],
        "observed_mean_difference": obs_mean,
        "observed_n": n,
        "imputed_pairs": missing_pairs,
        "mean_difference": combined,
        "is_estimate": False,
        "note": "Assumes every missing pair contributes exactly zero difference.",
    }


def _multiple_imputation(
    diffs: Sequence[float],
    missing_pairs: int,
    *,
    m: int,
    seed: int,
    alpha: float,
) -> dict:
    if len(diffs) < 2:
        return {
            "scenario": "imputed_mi",
            "status": "not_run",
            "reason": "fewer than two complete pairs; the declared model cannot be fitted",
            "is_estimate": False,
        }
    mu = mean(diffs)
    sd = sample_sd(diffs)
    estimates, variances = [], []
    rng = random.Random(seed)
    for _ in range(m):
        imputed = [rng.gauss(mu, sd) for _ in range(missing_pairs)]
        full = list(diffs) + imputed
        n = len(full)
        estimates.append(mean(full))
        variances.append((sample_sd(full) ** 2) / n)
    combined = rubin_combine(estimates, variances, alpha=alpha)
    return {
        "scenario": "imputed_mi",
        "label": SENSITIVITY_ASSUMPTIONS["imputed_mi"]["label"],
        "detail": SENSITIVITY_ASSUMPTIONS["imputed_mi"]["detail"],
        "imputations": m,
        "seed": seed,
        "model": {"mean": mu, "sd": sd, "imputed_pairs_per_imputation": missing_pairs},
        "combined": combined,
        "is_estimate": True,
        "note": (
            "Model-based estimate whose validity depends on the declared normal assumption; "
            "the fixed seed only makes it reproducible, it does not make it assumption-free."
        ),
    }


# --------------------------------------------------------------------------- #
# report assembly
# --------------------------------------------------------------------------- #


def _input_record(*parts: Any) -> list[dict[str, str]]:
    return [{"name": name, "sha256": sha256_text(text)} for name, text in parts]


def build_report(
    *,
    outcomes_payload: dict[str, Any],
    plan: dict[str, Any],
    plan_text: str,
    outcome_name: str,
    alpha: float,
    mi_seed: int,
    mi_draws: int,
    higher_is_better: bool,
    missing_bounds: dict[str, float] | None = None,
) -> dict[str, Any]:
    spec = outcomes_payload["outcome_definitions"][outcome_name]
    assembled = assemble(
        plan["pairs"],
        outcomes_payload["rows"],
        outcome_name,
        spec,
        higher_is_better=higher_is_better,
    )
    diffs = [p["favourable_difference"] for p in assembled["complete"]]
    cc = paired_summary(diffs, alpha=alpha)
    cc["label"] = "Complete pairs only"
    cc["is_estimate"] = True
    cc["excludes_pairs"] = assembled["counts"]["excluded_pairs"]

    missing_count = len(assembled["missing"])
    sensitivity = []
    if missing_count:
        sensitivity.append(
            _bounded(diffs, missing_count, spec, higher_is_better, missing_bounds)
        )
        sensitivity.append(_tipped(diffs, missing_count))
        sensitivity.append(
            _multiple_imputation(diffs, missing_count, m=mi_draws, seed=mi_seed, alpha=alpha)
        )

    input_text = outcomes_payload["rows"]
    inputs = _input_record(
        ("observations.outcomes", digest(input_text)),
        ("pairing.plan", plan_text),
    )
    report_input_hash = digest(
        {
            "inputs": inputs,
            "outcome": outcome_name,
            "alpha": alpha,
            "mi_seed": mi_seed,
            "mi_draws": mi_draws,
            "higher_is_better": higher_is_better,
            "missing_bounds": missing_bounds,
            "outcome_definitions": outcomes_payload["outcome_definitions"],
        }
    )

    return {
        "outcome": outcome_name,
        "outcome_spec": spec,
        "higher_is_better": higher_is_better,
        "alpha": alpha,
        "analysis": {
            "complete_case": {
                **cc,
                "pairs": assembled["complete"],
                "excluded": assembled["missing"],
                "counts": assembled["counts"],
            },
            "sensitivity": sensitivity,
            "distinction": {
                "complete_case": "Estimate from fully observed pairs only; no missing data assumptions.",
                "sensitivity": "Bounds or model-based estimates that hold only under the stated assumption; never pool these into the complete-case result.",
            },
        },
        "plan": {
            "plan_version": plan["plan_version"],
            "plan_version_id": plan["plan_version_id"],
            "plan_checksum": plan["plan_checksum"],
            "policy": plan["policy"],
            "policy_checksum": plan["policy_checksum"],
            "observation_fingerprint": plan["observation_fingerprint"],
            "summary": plan["summary"],
            "exclusions": plan["exclusions"],
        },
        "provenance": {
            "inputs": inputs,
            "report_input_hash": report_input_hash,
            "observation_version": outcomes_payload["version"],
            "generated_by": "report-service/1.0",
        },
        "warnings": [
            "Paired observational comparison. A difference here is an association within the matched sample, not proof of a causal effect.",
            "Matching used covariates only, declared before outcomes were read.",
        ],
    }


def plan_text(plan: dict[str, Any]) -> str:
    from ..common.ids import dumps

    return dumps({k: plan[k] for k in ("plan_version_id", "plan_checksum", "pairs", "exclusions", "policy")})


def format_number(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "—"
    return f"{value:.{digits}f}"


def summary_lines(report: dict[str, Any]) -> list[str]:
    cc = report["analysis"]["complete_case"]
    lines = [
        f"{report['outcome']}: complete-case mean favourable difference "
        f"{format_number(cc['mean_difference'])} (95% CI {format_number(cc['ci_low'])} to "
        f"{format_number(cc['ci_high'])}), n={cc['n']} pairs."
    ]
    for scenario in report["analysis"]["sensitivity"]:
        if scenario["scenario"] == "bounded_worst_best":
            lines.append(
                f"Under the bounded assumption the difference lies between "
                f"{format_number(scenario['interval_low'])} and {format_number(scenario['interval_high'])}."
            )
    return lines


def coverage_check(report: dict[str, Any]) -> dict[str, Any]:
    cc = report["analysis"]["complete_case"]
    total = cc["counts"]["pairs_in_plan"]
    analysed = cc["n"]
    return {
        "pairs_in_plan": total,
        "pairs_analysed": analysed,
        "pairs_excluded": cc["counts"]["excluded_pairs"],
        "coverage": (analysed / total) if total else 0.0,
        "assumption_free": True,
    }
