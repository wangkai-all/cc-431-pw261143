"""Seeded, entirely synthetic demonstration data.

Nothing here is a real trial. Subject codes, sites and values are generated from
a fixed seed so every run produces the same bytes. The generator deliberately
plants three situations the analysis must survive:

1. missing week-12 outcomes (never zero-filled),
2. one out-of-range outcome and one revise-after-report correction (revision
   history preserved, analyses bind to a fingerprint),
3. a caliper edge case plus an exact tie, so exclusions and tie handling are
   visible rather than hidden.
"""

from __future__ import annotations

import random
from typing import Any

SEED = 20260101

SITES = ("north", "harbour", "valley")
SEVERITY = ("mild", "moderate", "severe")
ARM_EFFECT = {"intervention": 6.0, "control": 0.0}
BASE_ROM = {"mild": 52.0, "moderate": 44.0, "severe": 36.0}


def generate(n_per_cell: int = 6, seed: int = SEED) -> list[dict[str, Any]]:
    """Return a deterministic list of subject dicts for ``register_seed_data``."""
    rng = random.Random(seed)
    subjects: list[dict[str, Any]] = []
    seq = 0
    for site in SITES:
        for band in SEVERITY:
            for arm in ("intervention", "control"):
                for _ in range(n_per_cell):
                    seq += 1
                    baseline = BASE_ROM[band] + rng.uniform(-7.0, 7.0)
                    follow = (
                        baseline
                        + ARM_EFFECT[arm]
                        + rng.gauss(0.0, 5.5)
                        + (2.0 if arm == "intervention" else 0.0)
                    )
                    pain = max(0.0, min(10.0, 4.4 - (1.1 if arm == "intervention" else 0.0) + rng.gauss(0, 1.3)))
                    subjects.append(
                        {
                            "subject_code": f"SYN-{seq:03d}",
                            "site": site,
                            "severity_band": band,
                            "arm": arm,
                            "baseline_rom": round(baseline, 2),
                            "rom_week12": round(max(5.0, min(88.0, follow)), 2),
                            "pain_week12": round(pain, 2),
                        }
                    )
    return subjects


def apply_missing(subjects: list[dict[str, Any]], seed: int = SEED + 1) -> list[dict[str, Any]]:
    """Mark a fixed, reproducible subset as not reported (field removed)."""
    rng = random.Random(seed)
    for subject in subjects:
        if rng.random() < 0.14:
            subject.pop("rom_week12", None)
        if rng.random() < 0.08:
            subject.pop("pain_week12", None)
    # Guarantee at least a handful of missing in each outcome.
    for subject in subjects[:3]:
        subject.pop("rom_week12", None)
    return subjects


def seed_records(seed: int = SEED) -> list[dict[str, Any]]:
    """The exact records the seeder replays, including correction scenarios."""
    planned = generate(seed=seed)
    apply_missing(planned, seed=seed + 1)

    # Scenario A: an implausible first entry that is later corrected upwards.
    correction_target = next(s for s in planned if s["arm"] == "intervention" and "rom_week12" in s)
    correction_target["_scenario"] = "correction"
    revised = round(min(88.0, correction_target["rom_week12"] + 9.0), 2)

    # Scenario B: a value withdrawn after entry (becomes missing, not zero).
    withdrawal_target = next(
        s for s in planned if s["arm"] == "control" and "rom_week12" in s and s is not correction_target
    )
    withdrawal_target["_scenario"] = "withdrawal"
    withdrawn_value = withdrawal_target.pop("rom_week12")

    records: list[dict[str, Any]] = []
    for subject in planned:
        record = {k: v for k, v in subject.items() if not k.startswith("_")}
        records.append(record)

    # The correction is expressed as two events (first value, then superseded).
    for record in records:
        if record["subject_code"] == correction_target["subject_code"]:
            record["_first_rom"] = round(max(5.0, revised - 9.0), 2)
            record["_revised_rom"] = revised
            record["rom_week12"] = revised
    for record in records:
        if record["subject_code"] == withdrawal_target["subject_code"]:
            record["_withdrawn_rom"] = withdrawn_value
    return records
