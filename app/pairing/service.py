"""Pairing service: versioned, locked plans over the covariate feed.

A plan version is written once and never edited. Changing the policy, or
rebuilding against a changed observation feed, requires an explicit new version
whose lineage names the one it supersedes and the human reason for the change.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from ..common.ids import digest, new_id, utcnow
from ..common.journal import Journal
from .engine import PairingError, build_plan
from .policy import MatchingPolicy, policy_from_dict


def _initial_state() -> dict[str, Any]:
    return {"plans": {}, "order": []}


def _apply(state: dict[str, Any], event: Any) -> dict[str, Any]:
    if event.kind != "plan.created":
        raise ValueError(f"unknown event kind: {event.kind}")
    plan = dict(event.payload)
    plan["created_at"] = event.at
    state["plans"][plan["plan_version_id"]] = plan
    state["order"].append(plan["plan_version_id"])
    return state


class PairingStore:
    def __init__(self, data_dir: Path):
        self.journal = Journal(Path(data_dir) / "pairing.jsonl")
        self._state = self.journal.replay(_apply, _initial_state())

    def get(self, version_id: str) -> dict[str, Any]:
        try:
            return dict(self._state["plans"][version_id])
        except KeyError as exc:
            raise KeyError(version_id) from exc

    def latest(self) -> dict[str, Any] | None:
        if not self._state["order"]:
            return None
        return self.get(self._state["order"][-1])

    def versions(self) -> list[dict[str, Any]]:
        out = []
        for vid in self._state["order"]:
            p = self._state["plans"][vid]
            out.append(
                {
                    "plan_version": p["plan_version"],
                    "plan_version_id": vid,
                    "created_at": p["created_at"],
                    "reason": p["reason"],
                    "supersedes": p["supersedes"],
                    "status": "locked",
                    "policy_checksum": p["policy_checksum"],
                    "observation_fingerprint": p["observation_fingerprint"],
                    "summary": p["summary"],
                    "plan_checksum": p["plan_checksum"],
                }
            )
        return out

    def create(
        self,
        *,
        candidates: list[dict[str, Any]],
        observation_fingerprint: str,
        observation_version: int,
        policy: MatchingPolicy,
        reason: str,
    ) -> dict[str, Any]:
        if not reason.strip():
            raise ValueError("creating a plan version requires a stated reason")
        with self.journal.lock:
            body = build_plan(candidates, policy)
            prior = self.latest()
            plan_version = (prior["plan_version"] + 1) if prior else 1
            plan_checksum = digest(
                {
                    "plan": body["pairs"],
                    "exclusions": body["exclusions"],
                    "policy_checksum": body["policy_checksum"],
                    "observation_fingerprint": observation_fingerprint,
                }
            )
            if prior and plan_checksum == prior["plan_checksum"]:
                raise ValueError(
                    "identical pairs and policy to the current version; "
                    "a new version must change something"
                )
            version_id = new_id("plan")
            self.journal.append(
                "plan.created",
                {
                    "plan_version_id": version_id,
                    "plan_version": plan_version,
                    "supersedes": prior["plan_version_id"] if prior else None,
                    "reason": reason.strip(),
                    "policy": policy.to_dict(),
                    "policy_checksum": body["policy_checksum"],
                    "observation_fingerprint": observation_fingerprint,
                    "observation_version": observation_version,
                    "pairs": body["pairs"],
                    "exclusions": body["exclusions"],
                    "summary": body["summary"],
                    "uses_outcomes": False,
                    "plan_checksum": plan_checksum,
                },
            )
            self._state = self.journal.replay(_apply, _initial_state())
        return self.get(version_id)


class CreatePlanBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    policy: dict[str, Any] = Field(
        default_factory=lambda: MatchingPolicy().to_dict()
    )
    reason: str = Field(min_length=1, max_length=1000)


def create_app(data_dir: Path, observations_url: str) -> FastAPI:
    import httpx

    store = PairingStore(Path(data_dir))
    client = httpx.Client(base_url=observations_url.rstrip("/"), timeout=10.0)
    from ..common.errors import install

    app = FastAPI(title="Pairing service", version="1.0")
    install(app)

    def _fetch_candidates() -> dict[str, Any]:
        response = client.get("/api/candidates")
        response.raise_for_status()
        return response.json()

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "service": "pairing", "versions": len(store.versions())}

    @app.get("/api/plans")
    def list_plans() -> dict[str, Any]:
        return {"versions": store.versions(), "latest": (store.latest() or {}).get("plan_version_id")}

    @app.get("/api/plans/latest")
    def latest_plan() -> dict[str, Any]:
        plan = store.latest()
        if plan is None:
            raise HTTPException(status_code=404, detail="no plan has been created")
        return plan

    @app.get("/api/plans/{version_id}")
    def get_plan(version_id: str) -> dict[str, Any]:
        try:
            return store.get(version_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"unknown plan version {version_id}") from exc

    @app.post("/api/plans", status_code=201)
    def create_plan(body: CreatePlanBody) -> dict[str, Any]:
        try:
            policy = policy_from_dict(body.policy)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        try:
            feed = _fetch_candidates()
        except Exception as exc:  # transport-level failure is not a data verdict
            raise HTTPException(status_code=503, detail=f"observation feed unavailable: {exc}") from exc
        try:
            return store.create(
                candidates=feed["candidates"],
                observation_fingerprint=feed["fingerprint"],
                observation_version=feed["version"],
                policy=policy,
                reason=body.reason,
            )
        except PairingError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    app.state.store = store
    return app
