"""Exercise the durable relay with real worker processes and measured checks."""

from __future__ import annotations

import json
import multiprocessing
import sqlite3
import tempfile
import time
from contextlib import closing
from pathlib import Path
from queue import Empty
from typing import Any

from relay import Relay

WORKERS = 12


class FailBeforeWriteProvider:
    """One run's synthetic provider failure; other processes stay healthy."""

    def __init__(self, delegate: Any) -> None:
        self.delegate = delegate

    def create_draft(
        self,
        *,
        external_key: str,
        asset: dict[str, Any],
    ) -> dict[str, str]:
        raise TimeoutError("injected failure before provider acceptance")

    def read(self, external_key: str) -> dict[str, str]:
        return self.delegate.read(external_key)

    def list_objects(self) -> list[dict[str, str]]:
        return self.delegate.list_objects()


def worker(
    db_path: str,
    provider_path: str,
    run_id: str,
    inject_failure: bool,
    start: Any,
    outcomes: Any,
) -> None:
    relay = Relay(Path(db_path), Path(provider_path))
    if inject_failure:
        relay.provider = FailBeforeWriteProvider(relay.provider)
    try:
        start.wait()
        receipt = relay.run_once(run_id)
    except Exception as error:
        outcomes.put(
            {
                "run_id": run_id,
                "result": "error",
                "error_type": type(error).__name__,
                "error": str(error),
            }
        )
    else:
        outcomes.put(
            {
                "run_id": run_id,
                "result": "ok",
                "verified": bool(receipt.get("verified")),
            }
        )


def sql_snapshot(db_path: Path) -> dict[str, Any]:
    with closing(sqlite3.connect(db_path)) as connection, connection:
        status_rows = connection.execute(
            """
            SELECT status, COUNT(*)
            FROM deployments
            GROUP BY status
            ORDER BY status
            """
        ).fetchall()
        receipt_rows, error_rows, total_rows = connection.execute(
            """
            SELECT
                SUM(receipt_json IS NOT NULL),
                SUM(error_json IS NOT NULL),
                COUNT(*)
            FROM deployments
            """
        ).fetchone()
    return {
        "statuses": {str(status): int(count) for status, count in status_rows},
        "rows": int(total_rows),
        "rows_with_receipts": int(receipt_rows or 0),
        "rows_with_errors": int(error_rows or 0),
    }


def expected_provider_keys(
    run_ids: list[str],
    payload: dict[str, Any],
) -> set[str]:
    return {
        f"{run_id}:{asset['asset_id']}"
        for run_id in run_ids
        for asset in payload["assets"]
    }


def read_provider_keys(relay: Relay) -> tuple[set[str], int]:
    objects = relay.provider.list_objects()
    keys = {str(obj["external_key"]) for obj in objects}
    return keys, len(objects)


def main() -> None:
    payload = json.loads(
        Path("fixtures/deployment_request_short.json").read_text()
    )

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        db_path = root / "deployments.db"
        provider_path = root / "fake-hubspot.json"
        relay = Relay(db_path, provider_path)
        run_ids = [
            relay.submit(f"campaign-deploy-{index:03d}", payload).run_id
            for index in range(WORKERS)
        ]
        isolated_failure_run = run_ids[0]
        expected_keys = expected_provider_keys(run_ids, payload)

        context = multiprocessing.get_context("spawn")
        start = context.Barrier(len(run_ids))
        outcomes = context.Queue()
        processes = [
            context.Process(
                target=worker,
                args=(
                    str(db_path),
                    str(provider_path),
                    run_id,
                    run_id == isolated_failure_run,
                    start,
                    outcomes,
                ),
            )
            for run_id in run_ids
        ]
        for process in processes:
            process.start()

        worker_results: list[dict[str, Any]] = []
        deadline = time.monotonic() + 45.0
        for _ in processes:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                worker_results.append(outcomes.get(timeout=remaining))
            except Empty:
                break
        for process in processes:
            process.join(timeout=10)

        failures: list[str] = []
        hanging = [process.pid for process in processes if process.is_alive()]
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        if hanging:
            failures.append(f"worker processes did not exit: {hanging}")
        bad_exits = [
            process.exitcode
            for process in processes
            if process.exitcode not in {0, None}
        ]
        if bad_exits:
            failures.append(f"worker processes exited abnormally: {bad_exits}")
        if len(worker_results) != len(run_ids):
            failures.append(
                f"received {len(worker_results)} of {len(run_ids)} worker outcomes"
            )

        results_by_run = {
            str(result["run_id"]): result for result in worker_results
        }
        if len(results_by_run) != len(worker_results):
            failures.append("a worker outcome was duplicated")

        before = sql_snapshot(db_path)
        print(f"worker processes             : {len(processes)}")
        print(f"approved assets per run      : {len(payload['assets'])}")
        print(f"expected provider keys       : {len(expected_keys)}")
        print(f"worker outcomes              : {json.dumps(worker_results, sort_keys=True)}")
        print(f"SQL before recovery          : {json.dumps(before, sort_keys=True)}")

        try:
            before_keys, before_object_count = read_provider_keys(relay)
        except Exception as error:
            failures.append(
                "provider unreadable before recovery: "
                f"{type(error).__name__}: {error}"
            )
            before_keys = set()
            before_object_count = -1
        print(f"provider objects before      : {before_object_count}")
        print(f"provider keys missing before : {len(expected_keys - before_keys)}")
        print(f"provider keys extra before   : {len(before_keys - expected_keys)}")

        expected_error_runs = {isolated_failure_run}
        observed_error_runs = {
            run_id
            for run_id, result in results_by_run.items()
            if result.get("result") == "error"
        }
        if observed_error_runs != expected_error_runs:
            failures.append(
                "worker errors differed from the one injected failure: "
                f"expected={sorted(expected_error_runs)} "
                f"observed={sorted(observed_error_runs)}"
            )

        for run_id, result in results_by_run.items():
            state = relay.get(run_id)
            if result.get("result") == "ok":
                if state["status"] != "done" or not result.get("verified"):
                    failures.append(
                        f"successful worker {run_id} lacks a verified done row"
                    )
            elif state["status"] == "done":
                failures.append(
                    f"worker error for {run_id} was hidden by a done receipt"
                )
        isolated_state = relay.get(isolated_failure_run)
        isolated_receipt = isolated_state.get("receipt") or {}
        if (
            isolated_state["status"] != "retryable"
            or isolated_state.get("error") is None
            or isolated_receipt.get("verified") is not False
        ):
            failures.append(
                "injected run failure was not durably visible and retryable"
            )

        print("recovery pass                : starting")
        Relay(db_path, provider_path).recover()
        after = sql_snapshot(db_path)
        print(f"SQL after recovery           : {json.dumps(after, sort_keys=True)}")

        try:
            after_keys, after_object_count = read_provider_keys(relay)
        except Exception as error:
            failures.append(
                "provider unreadable after recovery: "
                f"{type(error).__name__}: {error}"
            )
            after_keys = set()
            after_object_count = -1
        print(f"provider objects after       : {after_object_count}")
        print(f"provider keys missing after  : {len(expected_keys - after_keys)}")
        print(f"provider keys extra after    : {len(after_keys - expected_keys)}")

        nonterminal: dict[str, str] = {}
        reconciliation_failures: dict[str, list[dict[str, Any]]] = {}
        for run_id in run_ids:
            state = relay.get(run_id)
            if state["status"] != "done":
                nonterminal[run_id] = str(state["status"])
            audit = relay.audit(run_id)
            if not audit["verified"] or audit["issues"]:
                reconciliation_failures[run_id] = list(audit["issues"])
        if nonterminal:
            failures.append(f"runs remained nonterminal: {nonterminal}")
        if reconciliation_failures:
            failures.append(
                "live reconciliation failed: "
                f"{json.dumps(reconciliation_failures, sort_keys=True)}"
            )
        if after_keys != expected_keys:
            failures.append("final provider keys differ from payload-derived keys")
        if after_object_count != len(expected_keys):
            failures.append("final provider object count differs from expected keys")
        expected_after_statuses = {"done": len(run_ids)}
        if after["statuses"] != expected_after_statuses:
            failures.append(
                f"unexpected final SQL statuses: {after['statuses']}"
            )
        if after["rows_with_errors"]:
            failures.append("a recovered run retained a current SQL error")

        if failures:
            print("STRESS RESULT                : FAILED")
            for failure in failures:
                print(f"  - {failure}")
            raise SystemExit(1)
        print("STRESS RESULT                : PASSED")


if __name__ == "__main__":
    main()
