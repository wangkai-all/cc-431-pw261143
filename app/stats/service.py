"""Report service: resumable, hashed analysis runs over an explicit input set.

Resumability
------------
An analysis request names its inputs: the *current* observation fingerprint and
a specific immutable plan version. Those two are hashed into one input hash
before any arithmetic. Each stage checkpoint references that hash.

If the process dies mid-run, ``POST /api/analyses/{id}/resume`` recomputes the
input hash from the services. If it matches the checkpoint, the stored stage
outputs are reused. If it does not, resume refuses with a conflict and says what
changed -- it never silently analyses different bytes under the old run.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from ..common.ids import digest, dumps, new_id, utcnow
from ..common.journal import Journal
from ..common.ids import loads
from . import analysis as analysis_mod
from .statlib import paired_summary

STAGES = ("fetch_inputs", "assemble_pairs", "summarise", "rendered")


def _initial_state() -> dict[str, Any]:
    return {"runs": {}, "order": []}


def _apply(state: dict[str, Any], event: Any) -> dict[str, Any]:
    run_id = event.payload["run_id"]
    run = state["runs"].setdefault(run_id, {"run_id": run_id, "created_at": event.at, "checkpoints": {}})
    if event.kind == "analysis.requested":
        run.update(
            {
                "plan_version_id": event.payload["plan_version_id"],
                "outcome": event.payload["outcome"],
                "alpha": event.payload["alpha"],
                "mi_seed": event.payload["mi_seed"],
                "mi_draws": event.payload["mi_draws"],
                "higher_is_better": event.payload["higher_is_better"],
                "missing_bounds": event.payload.get("missing_bounds"),
                "status": "pending",
                "input_hash": None,
                "report": None,
                "error": None,
                "finished_at": None,
            }
        )
        state["order"].append(run_id)
    elif event.kind == "analysis.checkpoint":
        run["checkpoints"][event.payload["stage"]] = event.payload
        run["input_hash"] = event.payload["input_hash"]
        run["status"] = "running"
    elif event.kind == "analysis.completed":
        run["status"] = "complete"
        run["report"] = event.payload["report"]
        run["finished_at"] = event.at
        run["resumed_from"] = event.payload.get("resumed_from")
    elif event.kind == "analysis.failed":
        run["status"] = "failed"
        run["error"] = event.payload["error"]
        run["finished_at"] = event.at
        run["failed_stage"] = event.payload.get("stage")
    elif event.kind == "analysis.input_changed":
        run["status"] = "stale"
        run["stale_detail"] = event.payload["detail"]
    else:  # pragma: no cover
        raise ValueError(f"unknown event kind {event.kind}")
    return state


class ReportStore:
    def __init__(self, data_dir: Path):
        self.journal = Journal(Path(data_dir) / "reports.jsonl")
        self._state = self.journal.replay(_apply, _initial_state())

    def get(self, run_id: str) -> dict[str, Any]:
        if run_id not in self._state["runs"]:
            raise KeyError(run_id)
        return dict(self._state["runs"][run_id])

    def runs(self) -> list[dict[str, Any]]:
        return [self.get(rid) for rid in self._state["order"]]

    def _reload(self) -> None:
        self._state = self.journal.replay(_apply, _initial_state())


class AnalysisBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_version_id: str
    outcome: str
    alpha: float = Field(default=0.05, gt=0.0, lt=1.0)
    mi_seed: int = 424242
    mi_draws: int = Field(default=20, ge=2, le=200)
    higher_is_better: bool | None = None
    missing_bounds: dict[str, float] | None = None


class ResumeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str = ""


class FaultBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage: str
    enabled: bool = True


def create_app(data_dir: Path, observations_url: str, pairing_url: str) -> FastAPI:
    store = ReportStore(Path(data_dir))
    obs = httpx.Client(base_url=observations_url.rstrip("/"), timeout=10.0)
    pairing = httpx.Client(base_url=pairing_url.rstrip("/"), timeout=10.0)
    faults = {"enabled": False, "stage": "summarise"}
    from ..common.errors import install

    app = FastAPI(title="Report service", version="1.0")
    install(app)

    # -- input fetching and hashing ----------------------------------------- #

    def _fetch(plan_version_id: str, outcome: str) -> dict[str, Any]:
        try:
            o_resp = obs.get("/api/outcomes")
            o_resp.raise_for_status()
            p_resp = pairing.get(f"/api/plans/{plan_version_id}")
            p_resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise HTTPException(
                status_code=502, detail=f"upstream input unavailable: {exc.response.status_code}"
            ) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=503, detail=f"upstream input unavailable: {exc}") from exc
        outcomes = o_resp.json()
        plan = p_resp.json()
        if outcome not in outcomes["outcome_definitions"]:
            raise HTTPException(status_code=422, detail=f"unknown outcome {outcome}")
        return {"outcomes": outcomes, "plan": plan, "plan_text": dumps(plan)}

    def _input_hash(inputs: dict[str, Any], body: dict[str, Any]) -> str:
        return digest(
            {
                "observations": digest(inputs["outcomes"]["rows"]),
                "plan": digest(inputs["plan_text"]),
                "outcome": body["outcome"],
                "alpha": body["alpha"],
                "mi_seed": body["mi_seed"],
                "mi_draws": body["mi_draws"],
                "higher_is_better": body["higher_is_better"],
                "missing_bounds": body["missing_bounds"],
                "outcome_definitions": inputs["outcomes"]["outcome_definitions"],
            }
        )

    def _checkpoint(run_id: str, stage: str, input_hash: str, data: Any) -> None:
        store.journal.append(
            "analysis.checkpoint",
            {
                "run_id": run_id,
                "stage": stage,
                "input_hash": input_hash,
                "at": utcnow(),
                "data_sha256": digest(data),
                "data": data,
            },
        )
        store._reload()

    def _execute(run_id: str, body: dict[str, Any], resumed_from: str | None = None) -> dict[str, Any]:
        run = store.get(run_id)
        inputs = _fetch(body["plan_version_id"], body["outcome"])
        input_hash = _input_hash(inputs, body)

        prior_checkpoints = run["checkpoints"] if resumed_from else {}
        reused = [
            stage for stage in STAGES
            if stage in prior_checkpoints and prior_checkpoints[stage]["input_hash"] == input_hash
        ]
        recorded_hash = run.get("input_hash")
        if recorded_hash is None and prior_checkpoints:
            recorded_hash = next(iter(prior_checkpoints.values()))["input_hash"]
        if resumed_from and recorded_hash and recorded_hash != input_hash:
            store.journal.append(
                "analysis.input_changed",
                {
                    "run_id": run_id,
                    "detail": (
                        "the observation set or plan no longer matches the interrupted run; "
                        "start a new analysis instead of resuming"
                    ),
                },
            )
            store._reload()
            raise HTTPException(
                status_code=409,
                detail=(
                    "cannot resume: input hash changed since the interruption "
                    "(observations corrected or plan replaced). Start a new analysis."
                ),
            )

        def stage_data(stage: str) -> Any:
            if stage in reused:
                return prior_checkpoints[stage]["data"]
            if stage == "fetch_inputs":
                return {
                    "observation_version": inputs["outcomes"]["version"],
                    "plan_checksum": inputs["plan"]["plan_checksum"],
                }
            if stage == "assemble_pairs":
                return _stage_assemble(inputs, body)
            if stage == "summarise":
                return _stage_summarise(inputs, body)
            if stage == "rendered":
                return _build_report(inputs, body, stage_data("summarise"))
            raise ValueError(stage)

        report: dict[str, Any] | None = None
        try:
            for stage in STAGES:
                if faults["enabled"] and faults["stage"] == stage:
                    raise RuntimeError(f"injected interruption at stage {stage}")
                data = stage_data(stage)
                if stage not in reused:
                    _checkpoint(run_id, stage, input_hash, data)
                if stage == "rendered":
                    report = data
        except HTTPException:
            raise
        except Exception as exc:
            store.journal.append(
                "analysis.failed",
                {
                    "run_id": run_id,
                    "error": str(exc),
                    "stage": faults["stage"] if faults["enabled"] else None,
                },
            )
            store._reload()
            raise

        assert report is not None
        report["resumability"] = {
            "stages": list(STAGES),
            "reused_stages": reused,
            "input_hash": input_hash,
        }
        store.journal.append(
            "analysis.completed",
            {"run_id": run_id, "report": report, "resumed_from": resumed_from},
        )
        store._reload()
        return store.get(run_id)

    def _build_report(inputs: dict[str, Any], body: dict[str, Any], summary: dict[str, Any]) -> dict[str, Any]:
        report = analysis_mod.build_report(
            outcomes_payload=inputs["outcomes"],
            plan=inputs["plan"],
            plan_text=inputs["plan_text"],
            outcome_name=body["outcome"],
            alpha=body["alpha"],
            mi_seed=body["mi_seed"],
            mi_draws=body["mi_draws"],
            higher_is_better=_higher_is_better(inputs, body),
            missing_bounds=body["missing_bounds"],
        )
        report["coverage"] = analysis_mod.coverage_check(report)
        report["summary_lines"] = analysis_mod.summary_lines(report)
        report["stage_summary"] = summary
        report["provenance"]["report_input_hash"] = _input_hash(inputs, body)
        return report

    def _higher_is_better(inputs: dict[str, Any], body: dict[str, Any]) -> bool:
        if body["higher_is_better"] is not None:
            return bool(body["higher_is_better"])
        return bool(inputs["outcomes"]["outcome_definitions"][body["outcome"]]["higher_is_better"])

    def _stage_assemble(inputs: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        return {
            "pairs": inputs["plan"]["pairs"],
            "exclusions": inputs["plan"]["exclusions"],
            "rows": inputs["outcomes"]["rows"],
            "definitions": inputs["outcomes"]["outcome_definitions"],
        }

    def _stage_summarise(inputs: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        spec = inputs["outcomes"]["outcome_definitions"][body["outcome"]]
        assembled = analysis_mod.assemble(
            inputs["plan"]["pairs"],
            inputs["outcomes"]["rows"],
            body["outcome"],
            spec,
            higher_is_better=_higher_is_better(inputs, body),
        )
        diffs = [p["favourable_difference"] for p in assembled["complete"]]
        return {
            "counts": assembled["counts"],
            "complete_case": paired_summary(diffs, body["alpha"]),
            "excluded_pairs": assembled["missing"],
        }

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "service": "reports", "runs": len(store.runs())}

    @app.get("/api/analyses")
    def list_runs() -> dict[str, Any]:
        return {
            "runs": [
                {
                    "run_id": r["run_id"],
                    "status": r["status"],
                    "outcome": r["outcome"],
                    "plan_version_id": r["plan_version_id"],
                    "created_at": r["created_at"],
                    "finished_at": r.get("finished_at"),
                    "input_hash": r.get("input_hash"),
                }
                for r in store.runs()
            ]
        }

    @app.get("/api/analyses/{run_id}")
    def get_run(run_id: str) -> dict[str, Any]:
        try:
            return store.get(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"unknown run {run_id}") from exc

    @app.post("/api/analyses", status_code=201)
    def start(body: AnalysisBody) -> dict[str, Any]:
        run_id = new_id("run")
        payload = body.model_dump()
        store.journal.append("analysis.requested", {"run_id": run_id, **payload})
        store._reload()
        try:
            return _execute(run_id, payload)
        except HTTPException:
            raise
        except Exception as exc:  # pragma: no cover - surfaces as 500
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.delete("/api/faults")
    def clear_fault() -> dict[str, Any]:
        faults["enabled"] = False
        return dict(faults)

    @app.post("/api/analyses/{run_id}/resume")
    def resume(run_id: str, body: ResumeBody) -> dict[str, Any]:
        try:
            run = store.get(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"unknown run {run_id}") from exc
        if run["status"] not in ("failed", "running", "pending"):
            raise HTTPException(status_code=409, detail=f"run is {run['status']}; nothing to resume")
        payload = {
            "plan_version_id": run["plan_version_id"],
            "outcome": run["outcome"],
            "alpha": run["alpha"],
            "mi_seed": run["mi_seed"],
            "mi_draws": run["mi_draws"],
            "higher_is_better": run["higher_is_better"],
            "missing_bounds": run.get("missing_bounds"),
        }
        return _execute(run_id, payload, resumed_from=run_id)

    @app.post("/api/faults")
    def set_fault(body: FaultBody) -> dict[str, Any]:
        if body.stage not in STAGES:
            raise HTTPException(status_code=422, detail=f"stage must be one of {STAGES}")
        faults["enabled"] = body.enabled
        faults["stage"] = body.stage
        return dict(faults)

    @app.get("/api/faults")
    def get_fault() -> dict[str, Any]:
        return dict(faults)

    app.state.store = store
    return app
