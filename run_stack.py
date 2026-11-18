#!/usr/bin/env python3
"""Start all four processes (three services + gateway) and wait.

Each child is an ordinary, independently runnable process:

    python -m app.observations --port 8101 --data var/data
    python -m app.pairing      --port 8102 --data var/data --observations-url ...
    python -m app.stats        --port 8103 --data var/data --observations-url ... --pairing-url ...
    python -m app.web.gateway  --port 8100 ...

This script only orchestrates; it does not share state between them.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ALL = ("observations", "pairing", "stats", "gateway")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the paired-analysis stack")
    parser.add_argument("--data", type=Path, default=Path("var/data"))
    parser.add_argument("--obs-port", type=int, default=8101)
    parser.add_argument("--pair-port", type=int, default=8102)
    parser.add_argument("--report-port", type=int, default=8103)
    parser.add_argument("--web-port", type=int, default=8100)
    args = parser.parse_args(argv)

    args.data.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parent)}
    obs_url = f"http://127.0.0.1:{args.obs_port}"
    pair_url = f"http://127.0.0.1:{args.pair_port}"
    commands = {
        "observations": [sys.executable, "-m", "app.observations", "--port", str(args.obs_port), "--data", str(args.data)],
        "pairing": [sys.executable, "-m", "app.pairing", "--port", str(args.pair_port), "--data", str(args.data), "--observations-url", obs_url],
        "stats": [sys.executable, "-m", "app.stats", "--port", str(args.report_port), "--data", str(args.data), "--observations-url", obs_url, "--pairing-url", pair_url],
        "gateway": [sys.executable, "-m", "app.web.gateway", "--port", str(args.web_port), "--observations-url", obs_url, "--pairing-url", pair_url, "--reports-url", f"http://127.0.0.1:{args.report_port}"],
    }

    children: dict[str, subprocess.Popen] = {}
    stopping = False

    def stop(*_):
        nonlocal stopping
        if stopping:
            return
        stopping = True
        for child in children.values():
            child.terminate()
        for child in children.values():
            try:
                child.wait(timeout=8)
            except subprocess.TimeoutExpired:
                child.kill()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    for name in ALL:
        print(f"[stack] starting {name}", flush=True)
        children[name] = subprocess.Popen(commands[name], env=env)
        time.sleep(0.4)
        if children[name].poll() is not None:
            print(f"[stack] {name} exited early", file=sys.stderr)
            stop()
            return 1

    print(f"[stack] console: http://127.0.0.1:{args.web_port}", flush=True)
    while not stopping:
        for name, child in children.items():
            code = child.poll()
            if code is not None:
                print(f"[stack] {name} exited with {code}", file=sys.stderr)
                stop()
                return code or 1
        time.sleep(0.5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
