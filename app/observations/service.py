"""Observation registry: covariate records and revision-aware outcome values.

Design rules enforced here
--------------------------
* An observation is registered with matching covariates only. Outcome values are
  separate measurement records, so pairing can (and does) run without seeing them.
* Missing is the *absence* of a reported measurement. The API rejects ``null``
  and non-finite numbers rather than turning missingness into a numeric zero.
* Corrections never overwrite history: every change appends a revision, and a
  correction must carry a reason. Analyses bind to a fingerprint of the current
  revision set, so a later correction produces a different input, never a silent
  recomputation.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..common.ids import digest, dumps, new_id, sha256_text, utcnow
from ..common.journal import Journal

OUTCOMES: dict[str, dict[str, Any]] = {
    "rom_week12": {
        "label": "Ankle dorsiflexion ROM at week 12",
        "unit": "degrees",
        "min": 0.0,
        "max": 90.0,
        "higher_is_better": True,
        "primary": True,
    },
    "pain_week12": {
        "label": "Resting pain at week 12",
        "unit": "0-10 NRS",
        "min": 0.0,
        "max": 10.0,
        "higher_is_better": False,
        "primary": False,
    },
}

SITES = ("north", "harbour", "valley")
SEVERITY_BANDS = ("mild", "moderate", "severe")
ARMS = ("intervention", "control")


# --------------------------------------------------------------------------- #
# projection
# --------------------------------------------------------------------------- #


def _initial_state() -> dict[str, Any]:
    return {"observations": {}, "measurements": {}}


def _apply(state: dict[str, Any], event: Any) -> dict[str, Any]:
    kind = event.kind
    payload = event.payload
    if kind == "observation.registered":
        obs_id = payload["obs_id"]
        state["observations"][obs_id] = {
            "obs_id": obs_id,
            "subject_code": payload["subject_code"],
            "site": payload["site"],
            "severity_band": payload["severity_band"],
            "arm": payload["arm"],
            "baseline_rom": payload["baseline_rom"],
            "registration_seq": payload["registration_seq"],
            "registered_at": event.at,
        }
        state["measurements"][obs_id] = {}
    elif kind in ("measurement.recorded", "measurement.corrected", "measurement.withdrawn"):
        obs_id = payload["obs_id"]
        outcome = payload["outcome"]
        revisions = state["measurements"].setdefault(obs_id, {}).setdefault(outcome, [])
        revisions.append(
            {
                "measurement_id": payload["measurement_id"],
                "revision": len(revisions) + 1,
                "status": payload["status"],
                "value": payload.get("value"),
                "note": payload.get("note", ""),
                "recorded_at": event.at,
                "kind": kind,
            }
        )
    else:  # pragma: no cover - guards against an unknown log
        raise ValueError(f"unknown event kind: {kind}")
    return state


class ObservationStore:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.journal = Journal(self.data_dir / "observations.jsonl")
        self._state = self.journal.replay(_apply, _initial_state())

    # -- reads -------------------------------------------------------------- #

    def _rows(self) -> list[dict[str, Any]]:
        return [
            dict(self._state["observations"][oid])
            for oid in sorted(
                self._state["observations"],
                key=lambda o: (self._state["observations"][o]["registration_seq"], o),
            )
        ]

    def current_measurement(self, obs_id: str, outcome: str) -> dict[str, Any] | None:
        revisions = self._state["measurements"].get(obs_id, {}).get(outcome, [])
        return dict(revisions[-1]) if revisions else None

    def measurements_for(self, obs_id: str) -> dict[str, Any]:
        return {
            outcome: [dict(r) for r in revisions]
            for outcome, revisions in sorted(self._state["measurements"].get(obs_id, {}).items())
        }

    def observation(self, obs_id: str) -> dict[str, Any]:
        if obs_id not in self._state["observations"]:
            raise KeyError(obs_id)
        return dict(self._state["observations"][obs_id])

    @property
    def version(self) -> int:
        return self.journal.events()[-1].seq if self.journal.events() else 0

    def fingerprint(self) -> str:
        """Identity + current outcome revision ids. Pairing-relevant covariates included."""
        observations = self._rows()
        outcomes = {}
        for obs in observations:
            per_outcome = {}
            for outcome in OUTCOMES:
                current = self.current_measurement(obs["obs_id"], outcome)
                if current:
                    per_outcome[outcome] = {
                        "measurement_id": current["measurement_id"],
                        "revision": current["revision"],
                        "status": current["status"],
                        "value": current["value"],
                    }
                else:
                    per_outcome[outcome] = {"status": "absent"}
            outcomes[obs["obs_id"]] = per_outcome
        return digest({"observations": observations, "outcomes": outcomes})

    def candidates(self) -> dict[str, Any]:
        """Covariate-only view. Deliberately contains no outcome field of any kind."""
        rows = [
            {
                "obs_id": o["obs_id"],
                "site": o["site"],
                "severity_band": o["severity_band"],
                "arm": o["arm"],
                "baseline_rom": o["baseline_rom"],
                "registration_seq": o["registration_seq"],
                "registered_at": o["registered_at"],
            }
            for o in self._rows()
        ]
        return {
            "version": self.version,
            "fingerprint": sha256_text(dumps(rows)),
            "candidates": rows,
        }

    def outcomes_snapshot(self) -> dict[str, Any]:
        rows: dict[str, dict[str, Any]] = {}
        for obs in self._rows():
            per_outcome = {}
            for outcome in OUTCOMES:
                current = self.current_measurement(obs["obs_id"], outcome)
                if current is None:
                    per_outcome[outcome] = {"status": "absent"}
                else:
                    per_outcome[outcome] = {
                        "status": current["status"],
                        "value": current["value"],
                        "measurement_id": current["measurement_id"],
                        "revision": current["revision"],
                    }
            rows[obs["obs_id"]] = per_outcome
        return {
            "version": self.version,
            "fingerprint": self.fingerprint(),
            "generated_at": utcnow(),
            "outcome_definitions": OUTCOMES,
            "rows": rows,
        }

    def snapshot(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "fingerprint": self.fingerprint(),
            "generated_at": utcnow(),
            "outcome_definitions": OUTCOMES,
            "observations": [
                {**o, "measurements": self.measurements_for(o["obs_id"])} for o in self._rows()
            ],
        }

    # -- writes ------------------------------------------------------------- #

    def register(
        self,
        *,
        subject_code: str,
        site: str,
        severity_band: str,
        arm: str,
        baseline_rom: float,
    ) -> dict[str, Any]:
        if site not in SITES:
            raise ValueError(f"site must be one of {SITES}")
        if severity_band not in SEVERITY_BANDS:
            raise ValueError(f"severity_band must be one of {SEVERITY_BANDS}")
        if arm not in ARMS:
            raise ValueError(f"arm must be one of {ARMS}")
        if not math.isfinite(baseline_rom) or not (0 <= baseline_rom <= 90):
            raise ValueError("baseline_rom must be a finite number in [0, 90]")
        if any(
            o["subject_code"] == subject_code for o in self._state["observations"].values()
        ):
            raise ValueError(f"subject_code already registered: {subject_code}")
        obs_id = new_id("obs")
        with self.journal.lock:
            seq = len(self._state["observations"]) + 1
            self.journal.append(
                "observation.registered",
                {
                    "obs_id": obs_id,
                    "subject_code": subject_code,
                    "site": site,
                    "severity_band": severity_band,
                    "arm": arm,
                    "baseline_rom": round(float(baseline_rom), 3),
                    "registration_seq": seq,
                },
            )
            self._state = self.journal.replay(_apply, _initial_state())
        return self.observation(obs_id)

    def record_outcome(
        self, obs_id: str, outcome: str, value: float, note: str = ""
    ) -> dict[str, Any]:
        if outcome not in OUTCOMES:
            raise ValueError(f"unknown outcome: {outcome}")
        if obs_id not in self._state["observations"]:
            raise KeyError(obs_id)
        spec = OUTCOMES[outcome]
        value = float(value)
        if not math.isfinite(value):
            raise ValueError("outcome value must be finite; missing is not a value")
        if not (spec["min"] <= value <= spec["max"]):
            raise ValueError(
                f"{outcome} must lie in [{spec['min']}, {spec['max']}] {spec['unit']}"
            )
        with self.journal.lock:
            prior = self.current_measurement(obs_id, outcome)
            kind = "measurement.corrected" if prior else "measurement.recorded"
            self.journal.append(
                kind,
                {
                    "obs_id": obs_id,
                    "outcome": outcome,
                    "measurement_id": new_id("meas"),
                    "status": "reported",
                    "value": round(value, 4),
                    "note": note.strip(),
                },
            )
            self._state = self.journal.replay(_apply, _initial_state())
        return self.current_measurement(obs_id, outcome)  # type: ignore[return-value]

    def withdraw_outcome(self, obs_id: str, outcome: str, reason: str) -> dict[str, Any]:
        if outcome not in OUTCOMES:
            raise ValueError(f"unknown outcome: {outcome}")
        if obs_id not in self._state["observations"]:
            raise KeyError(obs_id)
        prior = self.current_measurement(obs_id, outcome)
        if prior is None:
            raise ValueError("no reported measurement to withdraw")
        if not reason.strip():
            raise ValueError("withdrawal requires a reason")
        with self.journal.lock:
            self.journal.append(
                "measurement.withdrawn",
                {
                    "obs_id": obs_id,
                    "outcome": outcome,
                    "measurement_id": new_id("meas"),
                    "status": "withdrawn",
                    "value": None,
                    "note": reason.strip(),
                },
            )
            self._state = self.journal.replay(_apply, _initial_state())
        return self.current_measurement(obs_id, outcome)  # type: ignore[return-value]


# --------------------------------------------------------------------------- #
# HTTP surface
# --------------------------------------------------------------------------- #


class RegisterBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject_code: str = Field(min_length=1, max_length=64)
    site: str
    severity_band: str
    arm: str
    baseline_rom: float

    @field_validator("baseline_rom", mode="before")
    @classmethod
    def _finite(cls, v: Any) -> Any:
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError("baseline_rom must be a number")
        if not math.isfinite(float(v)):
            raise ValueError("baseline_rom must be finite")
        return float(v)


class OutcomeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome: str
    value: float = Field(allow_inf_nan=False)
    note: str = ""

    @field_validator("value", mode="before")
    @classmethod
    def _no_bool(cls, v: Any) -> Any:
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError("value must be a finite number; omit the field for missing")
        return float(v)


class WithdrawBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=500)


def create_app(data_dir: Path) -> FastAPI:
    store = ObservationStore(Path(data_dir))
    from ..common.errors import install

    app = FastAPI(title="Observations service", version="1.0")
    install(app)

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "service": "observations", "version": store.version}

    @app.get("/api/observations")
    def list_observations() -> dict[str, Any]:
        return {
            "version": store.version,
            "fingerprint": store.fingerprint(),
            "observations": [
                {**o, "measurements": store.measurements_for(o["obs_id"])}
                for o in store._rows()
            ],
        }

    @app.get("/api/candidates")
    def candidates() -> dict[str, Any]:
        return store.candidates()

    @app.get("/api/outcomes")
    def outcomes() -> dict[str, Any]:
        return store.outcomes_snapshot()

    @app.get("/api/snapshot")
    def snapshot() -> dict[str, Any]:
        return store.snapshot()

    @app.post("/api/observations", status_code=201)
    def register(body: RegisterBody) -> dict[str, Any]:
        try:
            return store.register(**body.model_dump())
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/observations/{obs_id}/outcomes", status_code=201)
    def record(obs_id: str, body: OutcomeBody) -> dict[str, Any]:
        try:
            return store.record_outcome(obs_id, body.outcome, body.value, body.note)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"unknown observation {obs_id}") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/observations/{obs_id}/outcomes/{outcome}/withdraw", status_code=201)
    def withdraw(obs_id: str, outcome: str, body: WithdrawBody) -> dict[str, Any]:
        try:
            return store.withdraw_outcome(obs_id, outcome, body.reason)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"unknown observation {obs_id}") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    app.state.store = store
    return app
