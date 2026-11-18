"""Pairing policy: declared up front, in covariate language only.

The policy is validated against a fixed allowlist of fields. Nothing in here can
reference an outcome, and pairing never receives outcome data at all.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from ..common.ids import digest

ALLOWED_MATCH_FIELDS = ("site", "severity_band")
ALLOWED_BALANCE_FIELDS = ("baseline_rom",)


@dataclass(frozen=True)
class MatchingPolicy:
    """Calm, pre-declared matching rules.

    * ``match_fields``  -- covariates on which pairs must agree exactly.
    * ``balance_field`` -- continuous covariate minimised inside a cell.
    * ``caliper``       -- maximum absolute difference on ``balance_field``.
    * ``tie_break_seed``-- only breaks *exact* ties; cannot change a better match.
    """

    match_fields: tuple[str, ...] = ("site", "severity_band")
    balance_field: str = "baseline_rom"
    caliper: float = 8.0
    tie_break_seed: int = 20260101

    def __post_init__(self) -> None:
        unknown = [f for f in self.match_fields if f not in ALLOWED_MATCH_FIELDS]
        if not self.match_fields or unknown:
            raise ValueError(f"match_fields must be drawn from {ALLOWED_MATCH_FIELDS}")
        if self.balance_field not in ALLOWED_BALANCE_FIELDS:
            raise ValueError(f"balance_field must be one of {ALLOWED_BALANCE_FIELDS}")
        if not (0 < float(self.caliper) <= 90):
            raise ValueError("caliper must be a finite number in (0, 90]")
        if not isinstance(self.tie_break_seed, int):
            raise ValueError("tie_break_seed must be an integer")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def cell_fields(self) -> tuple[str, ...]:
        return tuple(self.match_fields)

    def checksum(self) -> str:
        return digest(self.to_dict())

    def describe(self) -> dict[str, Any]:
        return {
            "cells": list(self.match_fields),
            "balance": self.balance_field,
            "caliper": float(self.caliper),
            "tie_break": "exact ties only, reproducible by seed",
            "uses_outcomes": False,
        }


def policy_from_dict(raw: dict[str, Any]) -> MatchingPolicy:
    allowed = {"match_fields", "balance_field", "caliper", "tie_break_seed"}
    extra = set(raw) - allowed
    if extra:
        raise ValueError(f"unknown policy fields: {sorted(extra)}")
    return MatchingPolicy(
        match_fields=tuple(raw.get("match_fields", ("site", "severity_band"))),
        balance_field=str(raw.get("balance_field", "baseline_rom")),
        caliper=float(raw.get("caliper", 8.0)),
        tie_break_seed=int(raw.get("tie_break_seed", 20260101)),
    )
