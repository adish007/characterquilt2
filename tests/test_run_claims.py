from __future__ import annotations

import sqlite3
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from typing import Any, Callable

from relay import (
    ClaimLost,
    IdempotencyConflict,
    InjectedCrash,
    Relay,
    RunClaimed,
)


def payload(*, asset_count: int = 2) -> dict[str, Any]:
    return {
        "destination": "hubspot-marketing",
        "mode": "draft",
        "assets": [
            {
                "asset_id": f"asset-{index}",
                "source_sha256": f"digest-{index}",
                "type": "email",
                "display_name": f"Draft {index}",
            }
            for index in range(asset_count)
        ],
    }


def raw_claim(db_path: Path, run_id: str) -> tuple[str | None, float | None]:
    with closing(sqlite3.connect(db_path)) as connection, connection:
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
    token = str(row[0]) if row[0] is not None else None
    expiry = float(row[1]) if row[1] is not None else None
    return token, expiry


class FakeClock:
    def __init__(self, value: float = 1_000.0) -> None:
        self._value = value
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            return self._value

    def advance(self, seconds: float) -> None:
        with self._lock:
            self._value += seconds


class DelegatingProvider:
    def __init__(self, delegate: Any) -> None:
        self.delegate = delegate

    def create_draft(
        self,
        *,
        external_key: str,
        asset: dict[str, Any],
    ) -> dict[str, str]:
        return self.delegate.create_draft(
            external_key=external_key,
            asset=asset,
        )

    def read(self, external_key: str) -> dict[str, str]:
        return self.delegate.read(external_key)

    def list_objects(self) -> list[dict[str, str]]:
        return self.delegate.list_objects()


class BlockingProvider(DelegatingProvider):
    def __init__(self, delegate: Any) -> None:
        super().__init__(delegate)
        self.entered = threading.Event()
        self.release = threading.Event()
        self.create_calls = 0
        self._lock = threading.Lock()

    def create_draft(
        self,
        *,
        external_key: str,
        asset: dict[str, Any],
    ) -> dict[str, str]:
        with self._lock:
            self.create_calls += 1
            should_block = self.create_calls == 1
        if should_block:
            self.entered.set()
            if not self.release.wait(timeout=5):
                raise TimeoutError("test did not release blocked provider call")
        return super().create_draft(external_key=external_key, asset=asset)


class CountingProvider(DelegatingProvider):
    def __init__(self, delegate: Any) -> None:
        super().__init__(delegate)
        self.operation_count = 0

    def create_draft(
        self,
        *,
        external_key: str,
        asset: dict[str, Any],
    ) -> dict[str, str]:
        self.operation_count += 1
        return super().create_draft(external_key=external_key, asset=asset)

    def read(self, external_key: str) -> dict[str, str]:
        self.operation_count += 1
        return super().read(external_key)


class FailingProvider(DelegatingProvider):
    def __init__(
        self,
        delegate: Any,
        should_fail: Callable[[str], bool] | None = None,
    ) -> None:
        super().__init__(delegate)
        self.should_fail = should_fail or (lambda _external_key: True)

    def create_draft(
        self,
        *,
        external_key: str,
        asset: dict[str, Any],
    ) -> dict[str, str]:
        if self.should_fail(external_key):
            raise RuntimeError(f"provider refused {external_key}")
        return super().create_draft(external_key=external_key, asset=asset)


class RenewalRecordingProvider(DelegatingProvider):
    def __init__(
        self,
        delegate: Any,
        relay: Relay,
        clock: FakeClock,
        advance_seconds: float,
    ) -> None:
        super().__init__(delegate)
        self.relay = relay
        self.clock = clock
        self.advance_seconds = advance_seconds
        self.observations: list[tuple[float, float, str]] = []

    def _record_and_advance(self, external_key: str) -> None:
        run_id = external_key.split(":", 1)[0]
        claim_token, claim_expires_at = raw_claim(
            self.relay.db_path,
            run_id,
        )
        if claim_token is None or claim_expires_at is None:
            raise AssertionError("provider operation began without a claim")
        self.observations.append(
            (
                self.clock(),
                claim_expires_at,
                claim_token,
            )
        )
        self.clock.advance(self.advance_seconds)

    def create_draft(
        self,
        *,
        external_key: str,
        asset: dict[str, Any],
    ) -> dict[str, str]:
        self._record_and_advance(external_key)
        return super().create_draft(external_key=external_key, asset=asset)

    def read(self, external_key: str) -> dict[str, str]:
        self._record_and_advance(external_key)
        return super().read(external_key)


class RunClaimsTest(unittest.TestCase):
    claim_ttl = 10.0

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.db_path = self.root / "deployments.db"
        self.provider_path = self.root / "provider.json"
        self.clock = FakeClock()
        self.relay = Relay(
            self.db_path,
            self.provider_path,
            clock=self.clock,
            claim_ttl_seconds=self.claim_ttl,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def new_relay(self) -> Relay:
        return Relay(
            self.db_path,
            self.provider_path,
            clock=self.clock,
            claim_ttl_seconds=self.claim_ttl,
        )

    def submit(self, key: str, *, asset_count: int = 2) -> str:
        return self.relay.submit(key, payload(asset_count=asset_count)).run_id

    def test_recovery_picks_up_pending_work(self) -> None:
        request = payload()
        run_id = self.relay.submit("pending", request).run_id

        self.relay.recover()

        run = self.relay.get(run_id)
        self.assertEqual(run["status"], "done")
        self.assertIsNotNone(run["receipt"])
        self.assertEqual(
            len(self.relay.provider.list_objects()),
            len(request["assets"]),
        )

    def test_active_crashed_claim_is_skipped_then_recovered_after_expiry(
        self,
    ) -> None:
        request = payload()
        run_id = self.relay.submit("crashed", request).run_id

        with self.assertRaises(InjectedCrash):
            self.relay.run_once(
                run_id,
                crash_at="after_first_provider_write",
            )

        crashed = self.relay.get(run_id)
        self.assertEqual(crashed["status"], "running")
        crashed_token, crashed_expiry = raw_claim(self.db_path, run_id)
        self.assertIsNotNone(crashed_token)
        self.assertIsNotNone(crashed_expiry)
        assert crashed_expiry is not None
        self.assertGreater(crashed_expiry, self.clock())
        self.assertEqual(len(self.relay.provider.list_objects()), 1)

        self.new_relay().recover()
        still_active = self.relay.get(run_id)
        self.assertEqual(still_active["status"], "running")
        self.assertEqual(len(self.relay.provider.list_objects()), 1)

        self.clock.advance(
            crashed_expiry - self.clock()
        )
        self.new_relay().recover()

        recovered = self.relay.get(run_id)
        self.assertEqual(recovered["status"], "done")
        self.assertEqual(
            len(self.relay.provider.list_objects()),
            len(request["assets"]),
        )
        self.assertEqual(raw_claim(self.db_path, run_id), (None, None))

    def test_expired_worker_without_replacement_cannot_continue_or_commit(
        self,
    ) -> None:
        request = payload()
        run_id = self.relay.submit("self-expired", request).run_id
        provider = RenewalRecordingProvider(
            self.relay.provider,
            self.relay,
            self.clock,
            advance_seconds=self.claim_ttl,
        )
        self.relay.provider = provider

        with self.assertRaises(ClaimLost):
            self.relay.run_once(run_id)

        expired = self.relay.get(run_id)
        _, expired_at = raw_claim(self.db_path, run_id)
        self.assertEqual(expired["status"], "running")
        self.assertIsNotNone(expired_at)
        assert expired_at is not None
        self.assertLessEqual(expired_at, self.clock())
        self.assertIsNone(expired["receipt"])
        self.assertEqual(len(provider.observations), 1)
        self.assertEqual(len(self.relay.provider.list_objects()), 1)

    def test_claim_time_is_sampled_after_database_lock_is_acquired(self) -> None:
        run_id = self.submit("contended-claim", asset_count=0)
        blocker = sqlite3.connect(self.db_path)
        blocker.execute("BEGIN IMMEDIATE")
        connection_opened = threading.Event()
        original_connect = self.relay._connect
        outcome: list[tuple[str, Any]] = []

        def signaling_connect() -> sqlite3.Connection:
            connection = original_connect()
            connection_opened.set()
            return connection

        def execute() -> None:
            try:
                outcome.append(("ok", self.relay.run_once(run_id)))
            except BaseException as error:
                outcome.append(("error", error))

        self.relay._connect = signaling_connect  # type: ignore[method-assign]
        worker = threading.Thread(target=execute)
        try:
            worker.start()
            self.assertTrue(connection_opened.wait(timeout=2))
            self.clock.advance(self.claim_ttl + 1)
            blocker.commit()
            worker.join(timeout=2)
        finally:
            blocker.close()
            self.relay._connect = original_connect  # type: ignore[method-assign]
            if worker.is_alive():
                worker.join(timeout=5)

        self.assertFalse(worker.is_alive())
        self.assertEqual(outcome[0][0], "ok", outcome)
        self.assertEqual(self.relay.get(run_id)["status"], "done")

    def test_expired_claim_cannot_commit_after_waiting_for_database(self) -> None:
        run_id = self.submit("contended-completion", asset_count=0)
        claim_token = "contended-token"
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                """
                UPDATE deployments
                SET status = 'running', claim_token = ?, claim_expires_at = ?
                WHERE id = ?
                """,
                (claim_token, self.clock() + self.claim_ttl, run_id),
            )

        blocker = sqlite3.connect(self.db_path)
        blocker.execute("BEGIN IMMEDIATE")
        connection_opened = threading.Event()
        original_connect = self.relay._connect
        outcome: list[BaseException | str] = []

        def signaling_connect() -> sqlite3.Connection:
            connection = original_connect()
            connection_opened.set()
            return connection

        def complete() -> None:
            try:
                self.relay._complete(
                    run_id,
                    claim_token,
                    {"objects": [], "verified": True},
                )
            except BaseException as error:
                outcome.append(error)
            else:
                outcome.append("committed")

        self.relay._connect = signaling_connect  # type: ignore[method-assign]
        worker = threading.Thread(target=complete)
        try:
            worker.start()
            self.assertTrue(connection_opened.wait(timeout=2))
            self.clock.advance(self.claim_ttl + 1)
            blocker.commit()
            worker.join(timeout=2)
        finally:
            blocker.close()
            self.relay._connect = original_connect  # type: ignore[method-assign]
            if worker.is_alive():
                worker.join(timeout=5)

        self.assertFalse(worker.is_alive())
        self.assertEqual(len(outcome), 1)
        self.assertIsInstance(outcome[0], ClaimLost)
        run = self.relay.get(run_id)
        self.assertEqual(run["status"], "running")
        self.assertIsNone(run["receipt"])

    def test_two_workers_cannot_hold_valid_claims_for_one_run(self) -> None:
        run_id = self.submit("single-owner")
        contender = self.new_relay()
        blocking = BlockingProvider(self.relay.provider)
        self.relay.provider = blocking

        with ThreadPoolExecutor(max_workers=1) as executor:
            first = executor.submit(self.relay.run_once, run_id)
            self.assertTrue(blocking.entered.wait(timeout=2))
            claimed = raw_claim(self.db_path, run_id)

            with self.assertRaises(RunClaimed):
                contender.run_once(run_id)

            unchanged = raw_claim(self.db_path, run_id)
            self.assertEqual(unchanged, claimed)
            blocking.release.set()
            first.result(timeout=2)

    def test_claim_is_renewed_before_each_provider_operation(self) -> None:
        request = payload()
        run_id = self.relay.submit("renewed", request).run_id
        provider = RenewalRecordingProvider(
            self.relay.provider,
            self.relay,
            self.clock,
            advance_seconds=self.claim_ttl * 0.6,
        )
        self.relay.provider = provider

        self.relay.run_once(run_id)

        self.assertEqual(
            len(provider.observations),
            len(request["assets"]) * 2,
        )
        tokens = {token for _, _, token in provider.observations}
        self.assertEqual(len(tokens), 1)
        for observed_at, expires_at, _ in provider.observations:
            self.assertGreater(expires_at, observed_at)
            self.assertEqual(expires_at, observed_at + self.claim_ttl)

    def test_expired_worker_finishes_inflight_call_but_is_fenced_afterward(
        self,
    ) -> None:
        request = payload()
        run_id = self.relay.submit("fenced", request).run_id
        blocking = BlockingProvider(self.relay.provider)
        self.relay.provider = blocking
        replacement = self.new_relay()

        with ThreadPoolExecutor(max_workers=2) as executor:
            old_worker = executor.submit(self.relay.run_once, run_id)
            self.assertTrue(blocking.entered.wait(timeout=2))
            old_claim, _ = raw_claim(self.db_path, run_id)
            self.clock.advance(self.claim_ttl + 1)

            replacement_worker = executor.submit(replacement.run_once, run_id)
            deadline = time.monotonic() + 2
            while raw_claim(self.db_path, run_id)[0] == old_claim:
                if time.monotonic() >= deadline:
                    self.fail("replacement did not acquire the expired claim")
                time.sleep(0.01)

            blocking.release.set()
            with self.assertRaises(ClaimLost):
                old_worker.result(timeout=2)
            replacement_receipt = replacement_worker.result(timeout=2)

        final = self.relay.get(run_id)
        self.assertEqual(final["status"], "done")
        self.assertEqual(final["receipt"], replacement_receipt)
        self.assertEqual(blocking.create_calls, 1)
        self.assertEqual(
            len(self.relay.provider.list_objects()),
            len(request["assets"]),
        )

    def test_retry_returns_same_run_without_stealing_active_claim(self) -> None:
        run_id = self.submit("active-retry")
        retrier = self.new_relay()
        blocking = BlockingProvider(self.relay.provider)
        self.relay.provider = blocking

        with ThreadPoolExecutor(max_workers=1) as executor:
            worker = executor.submit(self.relay.run_once, run_id)
            self.assertTrue(blocking.entered.wait(timeout=2))
            before = raw_claim(self.db_path, run_id)

            with self.assertRaises(RunClaimed):
                retrier.retry(run_id)

            after = self.relay.get(run_id)
            self.assertEqual(after["id"], run_id)
            self.assertEqual(after["status"], "running")
            self.assertEqual(raw_claim(self.db_path, run_id), before)
            blocking.release.set()
            worker.result(timeout=2)

    def test_retry_and_recovery_serialize_on_one_claim(self) -> None:
        request = payload()
        run_id = self.relay.submit("retry-recovery-race", request).run_id
        self.relay.provider = FailingProvider(self.relay.provider)
        with self.assertRaises(RuntimeError):
            self.relay.run_once(run_id)

        retrier = self.new_relay()
        recoverer = self.new_relay()
        operation_lock = threading.Lock()
        create_calls = 0

        class TrackingProvider(DelegatingProvider):
            def create_draft(
                inner_self,
                *,
                external_key: str,
                asset: dict[str, Any],
            ) -> dict[str, str]:
                nonlocal create_calls
                with operation_lock:
                    create_calls += 1
                return super(TrackingProvider, inner_self).create_draft(
                    external_key=external_key,
                    asset=asset,
                )

        retrier.provider = TrackingProvider(retrier.provider)
        recoverer.provider = TrackingProvider(recoverer.provider)
        barrier = threading.Barrier(2)

        def retry() -> str | RunClaimed:
            barrier.wait()
            try:
                return retrier.retry(run_id)
            except RunClaimed as error:
                return error

        def recover() -> None:
            barrier.wait()
            recoverer.recover()

        with ThreadPoolExecutor(max_workers=2) as executor:
            retry_future = executor.submit(retry)
            recovery_future = executor.submit(recover)
            retry_result = retry_future.result(timeout=2)
            recovery_future.result(timeout=2)

        self.assertTrue(
            retry_result == run_id or isinstance(retry_result, RunClaimed)
        )
        self.assertEqual(self.relay.get(run_id)["status"], "done")
        self.assertEqual(create_calls, len(request["assets"]))
        self.assertEqual(
            len(self.relay.provider.list_objects()),
            len(request["assets"]),
        )

    def test_completed_run_is_idempotent_without_provider_calls(self) -> None:
        run_id = self.submit("already-done")
        receipt = self.relay.run_once(run_id)
        counter = CountingProvider(self.relay.provider)
        self.relay.provider = counter

        replayed_receipt = self.relay.run_once(run_id)

        self.assertEqual(replayed_receipt, receipt)
        self.assertEqual(counter.operation_count, 0)
        self.assertEqual(self.relay.get(run_id)["status"], "done")

    def test_recovery_never_reacquires_terminal_runs(self) -> None:
        statuses = ("done", "failed", "cancelled")
        run_ids = [self.submit(f"terminal-{status}") for status in statuses]
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            for run_id, status in zip(run_ids, statuses):
                connection.execute(
                    "UPDATE deployments SET status = ? WHERE id = ?",
                    (status, run_id),
                )
        counter = CountingProvider(self.relay.provider)
        self.relay.provider = counter

        self.relay.recover()

        self.assertEqual(counter.operation_count, 0)
        self.assertEqual(
            [self.relay.get(run_id)["status"] for run_id in run_ids],
            list(statuses),
        )

    def test_legacy_running_row_without_lease_is_recoverable(self) -> None:
        request = payload()
        run_id = self.relay.submit("legacy-running", request).run_id
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                """
                UPDATE deployments
                SET status = 'running',
                    claim_token = NULL,
                    claim_expires_at = NULL
                WHERE id = ?
                """,
                (run_id,),
            )

        self.relay.recover()

        self.assertEqual(self.relay.get(run_id)["status"], "done")
        self.assertEqual(
            len(self.relay.provider.list_objects()),
            len(request["assets"]),
        )

    def test_provider_exception_records_retryable_error_and_releases_claim(
        self,
    ) -> None:
        run_id = self.submit("provider-error")
        self.relay.provider = FailingProvider(self.relay.provider)

        with self.assertRaisesRegex(RuntimeError, "provider refused"):
            self.relay.run_once(run_id)

        run = self.relay.get(run_id)
        self.assertEqual(run["status"], "retryable")
        self.assertEqual(raw_claim(self.db_path, run_id), (None, None))
        self.assertIsInstance(run["error"], dict)
        self.assertEqual(run["error"]["type"], "RuntimeError")
        self.assertIn("provider refused", run["error"]["message"])
        self.assertTrue(run["error"]["retryable"])
        self.assertIsNone(run["receipt"])

        self.assertEqual(
            self.relay.deployment_summary(run_id)["error"],
            run["error"],
        )
        self.assertEqual(self.relay.retry(run_id), run_id)
        requeued = self.relay.get(run_id)
        self.assertEqual(requeued["status"], "pending")
        self.assertEqual(requeued["error"], run["error"])

    def test_provider_identity_conflict_is_terminal(self) -> None:
        request = payload(asset_count=1)
        run_id = self.relay.submit("provider-conflict", request).run_id
        conflicting_asset = dict(request["assets"][0])
        conflicting_asset["source_sha256"] = "different-content"
        self.relay.provider.create_draft(
            external_key=f"{run_id}:{conflicting_asset['asset_id']}",
            asset=conflicting_asset,
        )

        with self.assertRaises(IdempotencyConflict):
            self.relay.run_once(run_id)

        run = self.relay.get(run_id)
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["error"]["type"], "IdempotencyConflict")
        self.assertFalse(run["error"]["retryable"])
        self.assertIsNone(run["receipt"])
        self.assertEqual(raw_claim(self.db_path, run_id), (None, None))
        with self.assertRaisesRegex(RuntimeError, "terminally failed"):
            self.relay.retry(run_id)

    def test_retryable_error_clears_stale_success_receipt(self) -> None:
        run_id = self.submit("stale-receipt", asset_count=1)
        self.relay.run_once(run_id)
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                """
                UPDATE deployments
                SET status = 'pending', claim_token = NULL,
                    claim_expires_at = NULL
                WHERE id = ?
                """,
                (run_id,),
            )
        self.relay.provider = FailingProvider(self.relay.provider)

        with self.assertRaises(RuntimeError):
            self.relay.run_once(run_id)

        run = self.relay.get(run_id)
        self.assertEqual(run["status"], "retryable")
        self.assertIsNone(run["receipt"])
        summary = self.relay.deployment_summary(run_id)
        self.assertFalse(summary["verified"])
        self.assertEqual(summary["objects_deployed"], 0)

    def test_poison_run_does_not_block_later_pending_work(self) -> None:
        poison_id = self.submit("poison")
        healthy_request = payload()
        healthy_id = self.relay.submit("healthy", healthy_request).run_id
        self.relay.provider = FailingProvider(
            self.relay.provider,
            should_fail=lambda external_key: external_key.startswith(
                f"{poison_id}:"
            ),
        )

        self.relay.recover()

        poison = self.relay.get(poison_id)
        healthy = self.relay.get(healthy_id)
        self.assertEqual(poison["status"], "retryable")
        self.assertIsInstance(poison["error"], dict)
        self.assertEqual(healthy["status"], "done")
        self.assertEqual(
            len(self.relay.provider.list_objects()),
            len(healthy_request["assets"]),
        )

    def test_concurrent_constructors_migrate_legacy_schema_once(self) -> None:
        legacy_db = self.root / "legacy.db"
        legacy_provider = self.root / "legacy-provider.json"
        with closing(sqlite3.connect(legacy_db)) as connection, connection:
            connection.execute(
                """
                CREATE TABLE deployments (
                    id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    receipt_json TEXT
                )
                """
            )
        constructor_count = 6
        barrier = threading.Barrier(constructor_count)

        def construct(_index: int) -> Relay:
            barrier.wait()
            return Relay(
                legacy_db,
                legacy_provider,
                clock=self.clock,
                claim_ttl_seconds=self.claim_ttl,
            )

        with ThreadPoolExecutor(max_workers=constructor_count) as executor:
            relays = list(executor.map(construct, range(constructor_count)))

        self.assertEqual(len(relays), constructor_count)
        with closing(sqlite3.connect(legacy_db)) as connection, connection:
            columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(deployments)"
                ).fetchall()
            }
        self.assertTrue(
            {"error_json", "claim_token", "claim_expires_at"} <= columns
        )


if __name__ == "__main__":
    unittest.main()
