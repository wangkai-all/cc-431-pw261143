"""Service entrypoints.

Run each independently, e.g.::

    python -m app.observations.service --port 8101 --data var/data
    python -m app.pairing.service --port 8102 --observations-url http://127.0.0.1:8101
    python -m app.stats.service --port 8103 --observations-url http://127.0.0.1:8101 \
        --pairing-url http://127.0.0.1:8102

They never import each other's state; all coupling is HTTP.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn


def _base(service: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=f"{service} service")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--data", type=Path, default=Path("var/data"))
    return parser


def main_observations(argv: list[str] | None = None) -> None:
    parser = _base("observations")
    args = parser.parse_args(argv)
    uvicorn.run(create_observations(args.data), host=args.host, port=args.port, log_level="info")


def main_pairing(argv: list[str] | None = None) -> None:
    parser = _base("pairing")
    parser.add_argument("--observations-url", default="http://127.0.0.1:8101")
    args = parser.parse_args(argv)
    uvicorn.run(
        create_pairing(args.data, args.observations_url), host=args.host, port=args.port, log_level="info"
    )


def main_reports(argv: list[str] | None = None) -> None:
    parser = _base("reports")
    parser.add_argument("--observations-url", default="http://127.0.0.1:8101")
    parser.add_argument("--pairing-url", default="http://127.0.0.1:8102")
    args = parser.parse_args(argv)
    uvicorn.run(
        create_reports(args.data, args.observations_url, args.pairing_url),
        host=args.host,
        port=args.port,
        log_level="info",
    )


def create_observations(data_dir: Path):
    from .observations.service import create_app

    return create_app(data_dir)


def create_pairing(data_dir: Path, observations_url: str):
    from .pairing.service import create_app

    return create_app(data_dir, observations_url)


def create_reports(data_dir: Path, observations_url: str, pairing_url: str):
    from .stats.service import create_app

    return create_app(data_dir, observations_url, pairing_url)


if __name__ == "__main__":
    raise SystemExit("use: python -m app.services.observations|pairing|reports")
