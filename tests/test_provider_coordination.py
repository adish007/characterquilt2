from __future__ import annotations

import json
import multiprocessing
import os
import queue
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from typing import Any

from relay import Relay


def payload(prefix: str, *, asset_count: int = 3) -> dict[str, Any]:
    return {
        "destination": "hubspot-marketing",
        "mode": "draft",
        "assets": [
            {
                "asset_id": f"{prefix}-asset-{index}",
                "source_sha256": f"{prefix}-digest-{index}",
                "type": "email",
                "display_name": f"{prefix} Draft {index}",
            }
            for index in range(asset_count)
        ],
    }


class SharedClock:
    def __init__(self, value: Any) -> None:
        self.value = value

    def __call__(self) -> float:
        with self.value.get_lock():
            return float(self.value.value)

    def advance(self, seconds: float) -> None:
        with self.value.get_lock():
            self.value.value += seconds


class BlockingProvider:
    """Expose whether relay-owned provider calls overlap across processes."""

    def __init__(
        self,
        delegate: Any,
        entered: Any,
        active: Any,
        maximum_active: Any,
        release: Any,
    ) -> None:
        self.delegate = delegate
        self.entered = entered
        self.active = active
        self.maximum_active = maximum_active
        self.release = release

    def _enter(self) -> None:
        with self.entered.get_lock():
            self.entered.value += 1
        with self.active.get_lock():
            self.active.value += 1
            current = int(self.active.value)
        with self.maximum_active.get_lock():
            self.maximum_active.value = max(
                int(self.maximum_active.value),
                current,
            )

    def _wait(self) -> None:
        if not self.release.wait(timeout=10):
            raise TimeoutError("test did not release provider call")
        # Keep every provider method observably in flight long enough for an
        # unlocked sibling read or write to overlap deterministically.
        time.sleep(0.02)

    def _leave(self) -> None:
        with self.active.get_lock():
            self.active.value -= 1

    def create_draft(
        self,
        *,
        external_key: str,
        asset: dict[str, Any],
    ) -> dict[str, str]:
        self._enter()
        try:
            self._wait()
            return self.delegate.create_draft(
                external_key=external_key,
                asset=asset,
            )
        finally:
            self._leave()

    def read(self, external_key: str) -> dict[str, str]:
        self._enter()
        try:
            self._wait()
            return self.delegate.read(external_key)
        finally:
            self._leave()

    def list_objects(self) -> list[dict[str, str]]:
        return self.delegate.list_objects()


def run_worker(
    db_path: str,
    provider_path: str,
    run_id: str,
    ready: Any,
    start: Any,
    outcomes: Any,
    probe: tuple[Any, Any, Any, Any] | None = None,
    clock: SharedClock | None = None,
    claim_ttl_seconds: float = 30.0,
) -> None:
    relay = Relay(
        Path(db_path),
        Path(provider_path),
        clock=clock,
        claim_ttl_seconds=claim_ttl_seconds,
    )
    if probe is not None:
        relay.provider = BlockingProvider(relay.provider, *probe)
    ready.put(run_id)
    start.wait()
    try:
        receipt = relay.run_once(run_id)
    except BaseException as error:
        outcomes.put((run_id, "error", type(error).__name__, str(error)))
    else:
        outcomes.put((run_id, "ok", receipt, None))


def crash_while_holding_gate(
    db_path: str,
    provider_path: str,
    entered: Any,
) -> None:
    relay = Relay(Path(db_path), Path(provider_path))
    with relay._provider_coordinator.hold():
        entered.set()
        os._exit(23)


def acquire_gate_after_crash(
    db_path: str,
    provider_path: str,
    acquired: Any,
) -> None:
    relay = Relay(Path(db_path), Path(provider_path))
    with relay._provider_coordinator.hold():
        acquired.put("acquired")


def statuses(db_path: Path, run_ids: list[str]) -> dict[str, str]:
    placeholders = ",".join("?" for _ in run_ids)
    with closing(sqlite3.connect(db_path)) as connection:
        rows = connection.execute(
            f"SELECT id, status FROM deployments WHERE id IN ({placeholders})",
            run_ids,
        ).fetchall()
    return {str(run_id): str(status) for run_id, status in rows}


def raw_claim(db_path: Path, run_id: str) -> tuple[str | None, float | None]:
    with closing(sqlite3.connect(db_path)) as connection:
        row = connection.execute(
            """
            SELECT claim_token, claim_expires_at
            FROM deployments
            WHERE id = ?
            """,
            (run_id,),
        ).fetchone()
    if row is None:
        raise KeyError(run_id)
    token = None if row[0] is None else str(row[0])
    expiry = None if row[1] is None else float(row[1])
    return token, expiry


def wait_until(predicate: Any, *, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("condition was not reached before timeout")
        time.sleep(0.01)


def collect(outcomes: Any, count: int) -> list[tuple[Any, ...]]:
    results: list[tuple[Any, ...]] = []
    deadline = time.monotonic() + 10
    while len(results) < count:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError(
                f"received {len(results)} of {count} worker outcomes"
            )
        try:
            results.append(outcomes.get(timeout=remaining))
        except queue.Empty as error:
            raise AssertionError(
                f"received {len(results)} of {count} worker outcomes"
            ) from error
    return results


class ProviderCoordinationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.db_path = self.root / "deployments.db"
        self.provider_path = self.root / "provider.json"
        self.relay = Relay(self.db_path, self.provider_path)
        self.context = multiprocessing.get_context("spawn")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def process_probe(self) -> tuple[Any, Any, Any, Any]:
        return (
            self.context.Value("i", 0),
            self.context.Value("i", 0),
            self.context.Value("i", 0),
            self.context.Event(),
        )

    def start_processes(
        self,
        run_ids: list[str],
        *,
        probe: tuple[Any, Any, Any, Any] | None = None,
        clock: SharedClock | None = None,
        claim_ttl_seconds: float = 30.0,
    ) -> tuple[list[Any], Any, Any]:
        start = self.context.Event()
        ready = self.context.Queue()
        outcomes = self.context.Queue()
        processes = [
            self.context.Process(
                target=run_worker,
                args=(
                    str(self.db_path),
                    str(self.provider_path),
                    run_id,
                    ready,
                    start,
                    outcomes,
                    probe,
                    clock,
                    claim_ttl_seconds,
                ),
            )
            for run_id in run_ids
        ]
        for process in processes:
            process.start()
        collect(ready, len(processes))
        start.set()
        return processes, outcomes, start

    def assert_processes_clean(self, processes: list[Any]) -> None:
        for process in processes:
            process.join(timeout=10)
            self.assertFalse(process.is_alive(), "worker process hung")
            self.assertEqual(process.exitcode, 0)

    def test_process_exit_releases_provider_gate(self) -> None:
        entered = self.context.Event()
        crashed = self.context.Process(
            target=crash_while_holding_gate,
            args=(str(self.db_path), str(self.provider_path), entered),
        )
        crashed.start()
        self.assertTrue(entered.wait(timeout=10), "crashing worker got no gate")
        crashed.join(timeout=10)
        self.assertFalse(crashed.is_alive(), "crashing worker did not exit")
        self.assertEqual(crashed.exitcode, 23)

        acquired = self.context.Queue()
        waiter = self.context.Process(
            target=acquire_gate_after_crash,
            args=(str(self.db_path), str(self.provider_path), acquired),
        )
        waiter.start()
        try:
            self.assertEqual(acquired.get(timeout=10), "acquired")
            waiter.join(timeout=10)
            self.assertFalse(waiter.is_alive(), "released gate stayed locked")
            self.assertEqual(waiter.exitcode, 0)
        finally:
            if waiter.is_alive():
                waiter.terminate()
            waiter.join(timeout=2)

    def test_distinct_runs_share_one_file_without_lost_updates(self) -> None:
        requests = [payload(f"run-{index}") for index in range(4)]
        run_ids = [
            self.relay.submit(f"request-{index}", request).run_id
            for index, request in enumerate(requests)
        ]
        probe = self.process_probe()
        entered, _active, maximum_active, release = probe
        processes, outcomes, _start = self.start_processes(
            run_ids,
            probe=probe,
        )
        try:
            wait_until(
                lambda: statuses(self.db_path, run_ids)
                == {run_id: "running" for run_id in run_ids}
            )
            wait_until(lambda: int(entered.value) >= 1)
            time.sleep(0.2)
            self.assertEqual(
                int(entered.value),
                1,
                "more than one process entered the unsafe provider at once",
            )
        finally:
            release.set()

        results = collect(outcomes, len(run_ids))
        self.assert_processes_clean(processes)
        self.assertTrue(
            all(kind == "ok" for _run_id, kind, _value, _detail in results)
        )
        self.assertEqual({result[0] for result in results}, set(run_ids))
        self.assertEqual(int(maximum_active.value), 1)

        provider_document = json.loads(self.provider_path.read_text())
        expected_keys = {
            f"{run_id}:{asset['asset_id']}"
            for run_id, request in zip(run_ids, requests)
            for asset in request["assets"]
        }
        self.assertEqual(set(provider_document), expected_keys)
        self.assertEqual(len(provider_document), len(expected_keys))
        self.assertEqual(
            {obj["external_key"] for obj in provider_document.values()},
            expected_keys,
        )

        for run_id, request in zip(run_ids, requests):
            run = self.relay.get(run_id)
            self.assertEqual(run["status"], "done")
            receipt_keys = {
                obj["external_key"] for obj in run["receipt"]["objects"]
            }
            self.assertEqual(
                receipt_keys,
                {
                    f"{run_id}:{asset['asset_id']}"
                    for asset in request["assets"]
                },
            )

    def test_same_run_contention_has_one_owner_and_one_receipt(self) -> None:
        request = payload("same-run")
        run_id = self.relay.submit("same-run-request", request).run_id
        worker_count = 5
        probe = self.process_probe()
        entered, _active, maximum_active, release = probe
        processes, outcomes, _start = self.start_processes(
            [run_id] * worker_count,
            probe=probe,
        )
        try:
            wait_until(lambda: int(entered.value) == 1)
            early_results = collect(outcomes, worker_count - 1)
            self.assertTrue(
                all(
                    kind == "error" and value == "RunClaimed"
                    for _run_id, kind, value, _detail in early_results
                )
            )
        finally:
            release.set()

        final_results = early_results + collect(outcomes, 1)
        self.assert_processes_clean(processes)
        self.assertEqual(
            sum(kind == "ok" for _id, kind, _value, _detail in final_results),
            1,
        )
        self.assertEqual(int(maximum_active.value), 1)

        run = self.relay.get(run_id)
        self.assertEqual(run["status"], "done")
        provider_document = json.loads(self.provider_path.read_text())
        expected_keys = {
            f"{run_id}:{asset['asset_id']}" for asset in request["assets"]
        }
        self.assertEqual(set(provider_document), expected_keys)
        self.assertEqual(
            {obj["external_key"] for obj in run["receipt"]["objects"]},
            expected_keys,
        )

    def test_expired_inflight_worker_finishes_but_cannot_continue_or_commit(
        self,
    ) -> None:
        ttl = 5.0
        clock = SharedClock(self.context.Value("d", 1_000.0))
        request = payload("expired", asset_count=2)
        relay = Relay(
            self.db_path,
            self.provider_path,
            clock=clock,
            claim_ttl_seconds=ttl,
        )
        run_id = relay.submit("expired-request", request).run_id
        probe = self.process_probe()
        entered, _active, maximum_active, release = probe

        old_processes: list[Any] = []
        new_processes: list[Any] = []
        try:
            ready = self.context.Queue()
            old_start = self.context.Event()
            new_start = self.context.Event()
            old_outcomes = self.context.Queue()
            new_outcomes = self.context.Queue()
            old_processes = [
                self.context.Process(
                    target=run_worker,
                    args=(
                        str(self.db_path),
                        str(self.provider_path),
                        run_id,
                        ready,
                        old_start,
                        old_outcomes,
                        probe,
                        clock,
                        ttl,
                    ),
                )
            ]
            new_processes = [
                self.context.Process(
                    target=run_worker,
                    args=(
                        str(self.db_path),
                        str(self.provider_path),
                        run_id,
                        ready,
                        new_start,
                        new_outcomes,
                        None,
                        clock,
                        ttl,
                    ),
                )
            ]
            for process in old_processes + new_processes:
                process.start()
            collect(ready, 2)

            old_start.set()
            wait_until(lambda: int(entered.value) == 1)
            old_token, old_expiry = raw_claim(self.db_path, run_id)
            self.assertIsNotNone(old_token)
            self.assertIsNotNone(old_expiry)

            clock.advance(ttl + 1)
            new_start.set()
            wait_until(
                lambda: (
                    raw_claim(self.db_path, run_id)[0] is not None
                    and raw_claim(self.db_path, run_id)[0] != old_token
                )
            )
            release.set()

            old_result = collect(old_outcomes, 1)[0]
            new_result = collect(new_outcomes, 1)[0]
            self.assert_processes_clean(old_processes + new_processes)
        finally:
            release.set()
            for process in old_processes + new_processes:
                if process.is_alive():
                    process.terminate()
                process.join(timeout=2)

        self.assertEqual(old_result[1:3], ("error", "ClaimLost"))
        self.assertEqual(new_result[1], "ok")
        self.assertEqual(int(maximum_active.value), 1)
        self.assertEqual(int(_active.value), 0)

        run = relay.get(run_id)
        self.assertEqual(run["status"], "done")
        self.assertEqual(run["receipt"], new_result[2])
        provider_document = json.loads(self.provider_path.read_text())
        expected_keys = {
            f"{run_id}:{asset['asset_id']}" for asset in request["assets"]
        }
        self.assertEqual(set(provider_document), expected_keys)
        self.assertEqual(len(provider_document), len(request["assets"]))


if __name__ == "__main__":
    unittest.main()
