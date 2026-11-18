"""Seed the observation service over its own HTTP API.

The seeder is a *client*: it never touches the observation service's storage or
imports its state. It registers covariates first, then reports outcomes, so the
pairing service can be built before any outcome was ever visible.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

import httpx

from .seed import seed_records


def seed(base_url: str, timeout: float = 30.0) -> dict[str, Any]:
    records = seed_records()
    created: dict[str, str] = {}
    corrections: list[dict[str, Any]] = []
    with httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout) as client:
        existing = {o["subject_code"]: o["obs_id"] for o in client.get("/api/observations").json()["observations"]}
        for record in records:
            code = record["subject_code"]
            if code in existing:
                created[code] = existing[code]
                continue
            payload = {k: record[k] for k in ("subject_code", "site", "severity_band", "arm", "baseline_rom")}
            response = client.post("/api/observations", json=payload)
            response.raise_for_status()
            created[code] = response.json()["obs_id"]

        def record_outcome(obs_id: str, outcome: str, value: float, note: str) -> None:
            client.post(
                f"/api/observations/{obs_id}/outcomes",
                json={"outcome": outcome, "value": value, "note": note},
            ).raise_for_status()

        for record in records:
            obs_id = created[record["subject_code"]]
            if record.get("_first_rom") is not None:
                record_outcome(obs_id, "rom_week12", record["_first_rom"], "initial entry (synthetic)")
                second = client.post(
                    f"/api/observations/{obs_id}/outcomes",
                    json={
                        "outcome": "rom_week12",
                        "value": record["_revised_rom"],
                        "note": "correction after source-data review",
                    },
                )
                second.raise_for_status()
                corrections.append(
                    {"subject_code": record["subject_code"], "revision": second.json()["revision"]}
                )
            elif record.get("_withdrawn_rom") is not None:
                record_outcome(obs_id, "rom_week12", record["_withdrawn_rom"], "initial entry (synthetic)")
                client.post(
                    f"/api/observations/{obs_id}/outcomes/rom_week12/withdraw",
                    json={"reason": "site withdrew the value pending source verification"},
                ).raise_for_status()
            elif "rom_week12" in record:
                record_outcome(obs_id, "rom_week12", record["rom_week12"], "week-12 assessment")

            if "pain_week12" in record:
                record_outcome(obs_id, "pain_week12", record["pain_week12"], "week-12 assessment")

        final = client.get("/api/observations").json()
    return {
        "observations": len(created),
        "corrections": corrections,
        "version": final["version"],
        "fingerprint": final["fingerprint"],
        "missing_rom": sum(
            1
            for o in final["observations"]
            if not o["measurements"].get("rom_week12")
            or o["measurements"]["rom_week12"][-1]["status"] != "reported"
        ),
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Seed synthetic observation data over HTTP")
    parser.add_argument("--base-url", default="http://127.0.0.1:8101")
    args = parser.parse_args(argv)
    import json

    print(json.dumps(seed(args.base_url), indent=2))


if __name__ == "__main__":
    main(sys.argv[1:])
