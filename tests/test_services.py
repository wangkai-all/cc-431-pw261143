"""Integration tests: three real HTTP services in-process, no shared state."""

from __future__ import annotations

import socket
import threading
import time

import httpx
import pytest
import uvicorn

from app.common.seeder import seed
from app.observations.service import create_app as observations_app
from app.pairing.service import create_app as pairing_app
from app.stats.service import create_app as reports_app


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Server:
    def __init__(self, app, port):
        self.config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
        self.server = uvicorn.Server(self.config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        for _ in range(200):
            if self.server.started:
                return self
            time.sleep(0.02)
        raise RuntimeError("server did not start")

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(timeout=5)


@pytest.fixture()
def stack(tmp_path):
    ports = {name: free_port() for name in ("obs", "pair", "rep")}
    obs_url = f"http://127.0.0.1:{ports['obs']}"
    pair_url = f"http://127.0.0.1:{ports['pair']}"
    with (
        Server(observations_app(tmp_path / "obs"), ports["obs"]),
        Server(pairing_app(tmp_path / "pair", obs_url), ports["pair"]),
        Server(reports_app(tmp_path / "rep", obs_url, pair_url), ports["rep"]),
    ):
        yield {
            "obs": obs_url,
            "pair": pair_url,
            "rep": f"http://127.0.0.1:{ports['rep']}",
            "client": httpx.Client(timeout=30.0),
        }


def make_plan(stack, **policy):
    client = stack["client"]
    body: dict = {"reason": "test policy"}
    if policy:
        body["policy"] = policy
    response = client.post(f"{stack['pair']}/api/plans", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def run_analysis(stack, plan_id, **extra):
    client = stack["client"]
    body = {"plan_version_id": plan_id, "outcome": "rom_week12", **extra}
    return client.post(f"{stack['rep']}/api/analyses", json=body)


def test_candidates_endpoint_carries_no_outcome_field(stack):
    seed(stack["obs"])
    candidates = stack["client"].get(f"{stack['obs']}/api/candidates").json()
    assert candidates["candidates"]
    for row in candidates["candidates"]:
        assert not any("outcome" in k or k in {"value", "result"} for k in row)


def test_missing_is_absent_not_zero(stack):
    seed(stack["obs"])
    outcomes = stack["client"].get(f"{stack['obs']}/api/outcomes").json()
    statuses = {v["rom_week12"]["status"] for v in outcomes["rows"].values()}
    assert "absent" in statuses
    for row in outcomes["rows"].values():
        if row["rom_week12"]["status"] == "absent":
            assert "value" not in row["rom_week12"]


def test_null_and_non_finite_values_are_rejected(stack):
    client = stack["client"]
    created = client.post(
        f"{stack['obs']}/api/observations",
        json={
            "subject_code": "T-1",
            "site": "north",
            "severity_band": "mild",
            "arm": "intervention",
            "baseline_rom": 50.0,
        },
    ).json()
    for bad in (None, True, "50"):
        response = client.post(
            f"{stack['obs']}/api/observations/{created['obs_id']}/outcomes",
            json={"outcome": "rom_week12", "value": bad},
        )
        assert response.status_code == 422, (bad, response.text)
    raw = client.post(
        f"{stack['obs']}/api/observations/{created['obs_id']}/outcomes",
        content='{"outcome":"rom_week12","value":Infinity}',
        headers={"content-type": "application/json"},
    )
    assert raw.status_code == 422, raw.text


def test_correction_appends_revision_and_changes_fingerprint(stack):
    client = stack["client"]
    seed(stack["obs"])
    snapshot = client.get(f"{stack['obs']}/api/snapshot").json()
    target = next(
        o
        for o in snapshot["observations"]
        if o["measurements"].get("rom_week12")
        and o["measurements"]["rom_week12"][-1]["status"] == "reported"
        and len(o["measurements"]["rom_week12"]) == 1
    )
    before = snapshot["fingerprint"]
    response = client.post(
        f"{stack['obs']}/api/observations/{target['obs_id']}/outcomes",
        json={"outcome": "rom_week12", "value": 55.5, "note": "correction"},
    )
    assert response.status_code == 201
    assert response.json()["revision"] == 2
    after = client.get(f"{stack['obs']}/api/outcomes").json()
    assert after["fingerprint"] != before
    revisions = client.get(f"{stack['obs']}/api/observations").json()["observations"]
    row = next(o for o in revisions if o["obs_id"] == target["obs_id"])
    assert len(row["measurements"]["rom_week12"]) == 2
    assert row["measurements"]["rom_week12"][1]["value"] == 55.5
    assert row["measurements"]["rom_week12"][0]["value"] == target["measurements"]["rom_week12"][-1]["value"]


def test_plan_versions_are_immutable_and_reasoned(stack):
    seed(stack["obs"])
    first = make_plan(stack)
    # identical rebuild is refused
    response = stack["client"].post(f"{stack['pair']}/api/plans", json={"reason": "same again"})
    assert response.status_code == 409
    second = make_plan(stack, caliper=4.0)
    assert second["plan_version"] == 2
    assert second["supersedes"] == first["plan_version_id"]
    stored = stack["client"].get(f"{stack['pair']}/api/plans/{first['plan_version_id']}").json()
    assert stored["pairs"] == first["pairs"]
    assert stored["policy"]["caliper"] == 8.0


def test_analysis_binds_to_plan_and_observation_versions(stack):
    seed(stack["obs"])
    plan = make_plan(stack)
    run = run_analysis(stack, plan["plan_version_id"])
    assert run.status_code == 201, run.text
    report = run.json()["report"]
    assert report["plan"]["plan_version_id"] == plan["plan_version_id"]
    assert report["provenance"]["report_input_hash"]
    assert report["analysis"]["complete_case"]["n"] <= report["analysis"]["complete_case"]["counts"]["pairs_in_plan"]


def test_resume_reuses_checkpoints_and_recovers_after_fault(stack):
    seed(stack["obs"])
    plan = make_plan(stack)
    client = stack["client"]
    client.post(f"{stack['rep']}/api/faults", json={"stage": "summarise", "enabled": True})
    failed = run_analysis(stack, plan["plan_version_id"])
    assert failed.status_code >= 500 or failed.status_code == 503
    runs = client.get(f"{stack['rep']}/api/analyses").json()["runs"]
    run_id = runs[-1]["run_id"]
    assert runs[-1]["status"] == "failed"
    client.post(f"{stack['rep']}/api/faults", json={"stage": "summarise", "enabled": False})
    resumed = client.post(f"{stack['rep']}/api/analyses/{run_id}/resume", json={"note": "retry"})
    assert resumed.status_code == 200, resumed.text
    body = resumed.json()
    assert body["status"] == "complete"
    assert body["report"]["resumability"]["reused_stages"] == ["fetch_inputs", "assemble_pairs"]


def test_resume_refuses_when_observations_changed_midway(stack):
    seed(stack["obs"])
    plan = make_plan(stack)
    client = stack["client"]
    client.post(f"{stack['rep']}/api/faults", json={"stage": "summarise", "enabled": True})
    assert run_analysis(stack, plan["plan_version_id"]).status_code >= 500
    run_id = client.get(f"{stack['rep']}/api/analyses").json()["runs"][-1]["run_id"]
    client.post(f"{stack['rep']}/api/faults", json={"stage": "summarise", "enabled": False})

    snapshot = client.get(f"{stack['obs']}/api/snapshot").json()
    target = next(
        o for o in snapshot["observations"]
        if o["measurements"].get("rom_week12") and o["measurements"]["rom_week12"][-1]["status"] == "reported"
    )
    client.post(
        f"{stack['obs']}/api/observations/{target['obs_id']}/outcomes",
        json={"outcome": "rom_week12", "value": 33.0, "note": "late correction"},
    )
    refused = client.post(f"{stack['rep']}/api/analyses/{run_id}/resume", json={"note": "retry"})
    assert refused.status_code == 409
    assert "input hash changed" in refused.json()["detail"]


def test_correction_between_runs_changes_numbers_and_input_hash(stack):
    seed(stack["obs"])
    plan = make_plan(stack)
    client = stack["client"]
    first = run_analysis(stack, plan["plan_version_id"]).json()["report"]
    snapshot = client.get(f"{stack['obs']}/api/snapshot").json()
    target = next(
        o for o in snapshot["observations"]
        if o["measurements"].get("rom_week12") and o["measurements"]["rom_week12"][-1]["status"] == "reported"
    )
    client.post(
        f"{stack['obs']}/api/observations/{target['obs_id']}/outcomes",
        json={"outcome": "rom_week12", "value": 85.0, "note": "correction"},
    )
    second = run_analysis(stack, plan["plan_version_id"]).json()["report"]
    assert first["provenance"]["report_input_hash"] != second["provenance"]["report_input_hash"]
    assert first["analysis"]["complete_case"]["mean_difference"] != second["analysis"]["complete_case"]["mean_difference"]


def test_restart_preserves_state(tmp_path):
    ports = {name: free_port() for name in ("obs", "pair", "rep")}
    obs_url = f"http://127.0.0.1:{ports['obs']}"
    pair_url = f"http://127.0.0.1:{ports['pair']}"
    with Server(observations_app(tmp_path / "obs"), ports["obs"]):
        seed(obs_url)
        before = httpx.get(f"{obs_url}/api/outcomes").json()["fingerprint"]
    with (
        Server(observations_app(tmp_path / "obs"), ports["obs"]),
        Server(pairing_app(tmp_path / "pair", obs_url), ports["pair"]),
        Server(reports_app(tmp_path / "rep", obs_url, pair_url), ports["rep"]),
    ):
        after = httpx.get(f"{obs_url}/api/outcomes").json()["fingerprint"]
        assert after == before
        plan = httpx.post(f"{pair_url}/api/plans", json={"reason": "after restart"}).json()
        assert plan["plan_version"] == 1
