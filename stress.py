"""Drives the relay the way the production worker pool drives it.

Deployments are picked up by whichever worker is free, so several workers run
against the same database and the same provider at once. This script does
that and then reports what the provider is holding afterwards.
"""
from __future__ import annotations

import json
import tempfile
import threading
from pathlib import Path

from relay import Relay

WORKERS = 12


def main() -> None:
    payload = json.loads(Path("fixtures/deployment_request.json").read_text())
    assets = len(payload["assets"])

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        db_path = root / "deployments.db"
        provider_path = root / "fake-hubspot.json"

        relay = Relay(db_path, provider_path)
        run_ids = [
            relay.submit(f"campaign-deploy-{i:03d}", payload)
            for i in range(WORKERS)
        ]

        barrier = threading.Barrier(WORKERS)
        errors: list[str] = []

        def worker(run_id: str) -> None:
            worker_relay = Relay(db_path, provider_path)
            barrier.wait()
            try:
                worker_relay.run_once(run_id)
            except Exception as error:
                errors.append(f"{type(error).__name__}: {error}")

        threads = [
            threading.Thread(target=worker, args=(run_id,))
            for run_id in run_ids
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        print(f"workers                  : {WORKERS}")
        print(f"approved assets per run  : {assets}")
        print(f"objects the runs created : {WORKERS * assets}")
        try:
            held = len(relay.provider.list_objects())
            print(f"objects the provider holds: {held}")
        except Exception as error:
            print(f"objects the provider holds: unreadable ({error})")
            held = None

        statuses: dict[str, int] = {}
        for run_id in run_ids:
            status = relay.get(run_id)["status"]
            statuses[status] = statuses.get(status, 0) + 1
        print(f"run status counts        : {statuses}")
        print(f"errors raised            : {len(errors)}")
        for message in errors[:5]:
            print(f"  {message}")

        print()
        print("recovery pass over anything left running:")
        for attempt in range(3):
            try:
                Relay(db_path, provider_path).recover()
                print(f"  attempt {attempt + 1}: returned")
            except Exception as error:
                print(f"  attempt {attempt + 1}: {type(error).__name__}: {error}")

        after: dict[str, int] = {}
        for run_id in run_ids:
            status = relay.get(run_id)["status"]
            after[status] = after.get(status, 0) + 1
        print(f"run status counts after  : {after}")


if __name__ == "__main__":
    main()
