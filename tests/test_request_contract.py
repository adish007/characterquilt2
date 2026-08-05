from __future__ import annotations

import copy
import hashlib
import json
import multiprocessing
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from typing import Any

from relay import (
    IdempotencyConflict,
    InjectedCrash,
    Relay,
    RequestValidationError,
)


def submit_from_process(
    db_path: str,
    provider_path: str,
    key: str,
    payload: dict[str, Any],
    ready: Any,
    start: Any,
    results: Any,
) -> None:
    ready_sent = False
    try:
        relay = Relay(Path(db_path), Path(provider_path))
        ready.put(True)
        ready_sent = True
        if not start.wait(timeout=30):
            raise TimeoutError("submission start was not released")
        submission = relay.submit(key, payload)
        results.put(("ok", submission.run_id, submission.replayed))
    except BaseException as error:
        if not ready_sent:
            ready.put(False)
        results.put(("error", type(error).__name__, str(error)))


def valid_payload(*, asset_count: int = 2) -> dict[str, Any]:
    return {
        "destination": "hubspot-marketing",
        "mode": "draft",
        "assets": [
            {
                "asset_id": f"asset-{index}",
                "source_sha256": f"source-digest-{index}",
                "type": "email",
                "display_name": f"Approved draft {index}",
            }
            for index in range(asset_count)
        ],
    }


def canonical_payload(payload: dict[str, Any]) -> tuple[str, str]:
    payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return payload_json, hashlib.sha256(payload_json.encode()).hexdigest()


class RequestContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.db_path = self.root / "deployments.db"
        self.provider_path = self.root / "provider.json"
        self.now = 1_000.0
        self.claim_ttl = 10.0
        self.relay = Relay(
            self.db_path,
            self.provider_path,
            clock=lambda: self.now,
            claim_ttl_seconds=self.claim_ttl,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def deployment_count(self, *, idempotency_key: str | None = None) -> int:
        query = "SELECT COUNT(*) FROM deployments"
        parameters: tuple[str, ...] = ()
        if idempotency_key is not None:
            query += " WHERE idempotency_key = ?"
            parameters = (idempotency_key,)
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            return int(connection.execute(query, parameters).fetchone()[0])

    def assert_rejected_without_effects(
        self,
        key: str,
        payload: dict[str, Any],
    ) -> None:
        before_rows = self.deployment_count()
        before_objects = self.relay.provider.list_objects()

        with self.assertRaises(RequestValidationError):
            self.relay.submit(key, payload)

        self.assertEqual(self.deployment_count(), before_rows)
        self.assertEqual(self.deployment_count(idempotency_key=key), 0)
        self.assertEqual(self.relay.provider.list_objects(), before_objects)

    def test_explicit_empty_assets_is_a_verified_no_op(self) -> None:
        payload = valid_payload(asset_count=0)

        submission = self.relay.submit("empty-approval", payload)
        receipt = self.relay.run_once(submission.run_id)

        self.assertFalse(submission.replayed)
        self.assertEqual(submission.matching_payload_run_ids, ())
        self.assertEqual(self.relay.get(submission.run_id)["status"], "done")
        self.assertTrue(receipt["verified"])
        self.assertEqual(receipt["objects"], [])
        self.assertEqual(self.relay.provider.list_objects(), [])

    def test_missing_or_non_list_assets_is_rejected_and_key_is_reusable(
        self,
    ) -> None:
        invalid_payloads = [
            {
                "destination": "hubspot-marketing",
                "mode": "draft",
            },
            {
                "destination": "hubspot-marketing",
                "mode": "draft",
                "assets": None,
            },
            {
                "destination": "hubspot-marketing",
                "mode": "draft",
                "assets": {},
            },
            {
                "destination": "hubspot-marketing",
                "mode": "draft",
                "assets": "asset-0",
            },
        ]

        for index, payload in enumerate(invalid_payloads):
            key = f"invalid-assets-{index}"
            with self.subTest(payload=payload):
                self.assert_rejected_without_effects(key, payload)
                corrected = self.relay.submit(key, valid_payload())
                self.assertFalse(corrected.replayed)
                self.assertEqual(
                    self.deployment_count(idempotency_key=key),
                    1,
                )

    def test_key_and_payload_must_have_supported_top_level_shape(self) -> None:
        with self.assertRaises(RequestValidationError):
            self.relay.submit("   ", valid_payload())
        with self.assertRaises(RequestValidationError):
            self.relay.submit("not-an-object", [])  # type: ignore[arg-type]

        self.assertEqual(self.deployment_count(), 0)
        self.assertEqual(self.relay.provider.list_objects(), [])

    def test_every_asset_requires_non_empty_string_fields(self) -> None:
        required_fields = (
            "asset_id",
            "source_sha256",
            "type",
            "display_name",
        )
        invalid_values: tuple[Any, ...] = (None, "", "   ", 7)

        for field in required_fields:
            missing = valid_payload()
            del missing["assets"][0][field]
            with self.subTest(field=field, value="missing"):
                self.assert_rejected_without_effects(
                    f"missing-{field}",
                    missing,
                )

            for index, value in enumerate(invalid_values):
                invalid = valid_payload()
                invalid["assets"][0][field] = value
                with self.subTest(field=field, value=value):
                    self.assert_rejected_without_effects(
                        f"invalid-{field}-{index}",
                        invalid,
                    )

    def test_duplicate_asset_ids_are_rejected_before_writing(self) -> None:
        payload = valid_payload()
        payload["assets"][1]["asset_id"] = payload["assets"][0]["asset_id"]

        self.assert_rejected_without_effects("duplicate-assets", payload)

    def test_only_the_supported_destination_and_mode_are_accepted(self) -> None:
        changes = (
            ("destination", "another-destination"),
            ("mode", "published"),
        )

        for field, value in changes:
            payload = valid_payload()
            payload[field] = value
            with self.subTest(field=field, value=value):
                self.assert_rejected_without_effects(
                    f"unsupported-{field}",
                    payload,
                )

    def test_known_display_name_changes_are_rejected_before_writing(self) -> None:
        changed_names = (
            " padded name",
            "padded name ",
            "x" * (self.relay.provider.DISPLAY_NAME_LIMIT + 1),
        )

        for index, display_name in enumerate(changed_names):
            payload = valid_payload()
            payload["assets"][0]["display_name"] = display_name
            with self.subTest(display_name=display_name):
                self.assert_rejected_without_effects(
                    f"changed-name-{index}",
                    payload,
                )

    def test_main_fixture_is_intentionally_rejected_without_writes(self) -> None:
        payload = json.loads(
            Path("fixtures/deployment_request.json").read_text()
        )

        self.assert_rejected_without_effects("main-campaign", payload)

    def test_same_key_and_canonical_payload_returns_one_run(self) -> None:
        key = "stable-request"
        payload = valid_payload()
        reordered = {
            "assets": [
                {
                    "display_name": asset["display_name"],
                    "type": asset["type"],
                    "source_sha256": asset["source_sha256"],
                    "asset_id": asset["asset_id"],
                }
                for asset in payload["assets"]
            ],
            "mode": payload["mode"],
            "destination": payload["destination"],
        }

        first = self.relay.submit(key, payload)
        replay = self.relay.submit(key, reordered)

        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.run_id, first.run_id)
        self.assertEqual(self.deployment_count(idempotency_key=key), 1)

    def test_existing_legacy_binding_is_resolved_before_new_validation(
        self,
    ) -> None:
        payload = json.loads(
            Path("fixtures/deployment_request.json").read_text()
        )
        payload_json, payload_hash = canonical_payload(payload)
        with closing(sqlite3.connect(self.db_path)) as connection:
            with connection:
                connection.execute(
                    """
                    INSERT INTO deployments
                        (id, idempotency_key, payload_hash, payload_json, status)
                    VALUES ('legacy-run', 'legacy-key', ?, ?, 'running')
                    """,
                    (payload_hash, payload_json),
                )

        replay = self.relay.submit("legacy-key", copy.deepcopy(payload))

        self.assertTrue(replay.replayed)
        self.assertEqual(replay.run_id, "legacy-run")
        self.assertEqual(self.deployment_count(idempotency_key="legacy-key"), 1)

    def test_changed_invalid_payload_conflicts_with_existing_binding(self) -> None:
        self.relay.submit("bound-key", valid_payload())

        with self.assertRaises(IdempotencyConflict):
            self.relay.submit("bound-key", {"assets": "invalid"})

        self.assertEqual(self.deployment_count(idempotency_key="bound-key"), 1)

    def test_same_key_with_different_payload_conflicts_without_new_row(self) -> None:
        key = "immutable-request"
        original = valid_payload()
        changed = copy.deepcopy(original)
        changed["assets"][0]["source_sha256"] += "-changed"
        first = self.relay.submit(key, original)

        with self.assertRaises(IdempotencyConflict):
            self.relay.submit(key, changed)

        self.assertEqual(self.deployment_count(idempotency_key=key), 1)
        self.assertEqual(self.relay.provider.list_objects(), [])
        self.assertEqual(self.relay.get(first.run_id)["payload"], original)

    def test_different_key_same_payload_is_allowed_and_disclosed(self) -> None:
        payload = valid_payload()
        first = self.relay.submit("first-intent", payload)
        second = self.relay.submit("second-intent", copy.deepcopy(payload))

        self.assertNotEqual(second.run_id, first.run_id)
        self.assertFalse(second.replayed)
        self.assertEqual(
            set(second.matching_payload_run_ids),
            {first.run_id},
        )
        self.assertEqual(self.deployment_count(), 2)

    def test_repeated_retry_reuses_run_and_effect_namespace(self) -> None:
        payload = valid_payload()
        submission = self.relay.submit("retry-stable", payload)
        self.relay.run_once(submission.run_id)

        for _ in range(3):
            self.assertEqual(self.relay.retry(submission.run_id), submission.run_id)

        self.assertEqual(self.deployment_count(), 1)
        self.assertEqual(
            len(self.relay.provider.list_objects()),
            len(payload["assets"]),
        )

    def test_retry_preserves_interrupted_run_for_recovery(self) -> None:
        payload = valid_payload()
        submission = self.relay.submit("retry-stuck", payload)

        with self.assertRaises(InjectedCrash):
            self.relay.run_once(
                submission.run_id,
                crash_at="after_first_provider_write",
            )

        self.assertEqual(self.relay.get(submission.run_id)["status"], "running")
        self.now += self.claim_ttl
        self.assertEqual(self.relay.retry(submission.run_id), submission.run_id)
        self.assertEqual(self.relay.get(submission.run_id)["status"], "pending")
        self.relay.run_once(submission.run_id)

        self.assertEqual(self.deployment_count(), 1)
        self.assertEqual(
            len(self.relay.provider.list_objects()),
            len(payload["assets"]),
        )

    def test_legacy_invalid_run_is_rejected_before_another_provider_write(
        self,
    ) -> None:
        payload = json.loads(
            Path("fixtures/deployment_request.json").read_text()
        )
        payload_json, payload_hash = canonical_payload(payload)
        with closing(sqlite3.connect(self.db_path)) as connection:
            with connection:
                connection.execute(
                    """
                    INSERT INTO deployments
                        (id, idempotency_key, payload_hash, payload_json, status)
                    VALUES (
                        'legacy-invalid',
                        'legacy-invalid-key',
                        ?,
                        ?,
                        'running'
                    )
                    """,
                    (payload_hash, payload_json),
                )
        first_asset = payload["assets"][0]
        self.relay.provider.create_draft(
            external_key=f"legacy-invalid:{first_asset['asset_id']}",
            asset=first_asset,
        )
        provider_before_recovery = self.relay.provider.list_objects()

        self.relay.recover()

        self.assertEqual(
            self.relay.provider.list_objects(),
            provider_before_recovery,
        )
        run = self.relay.get("legacy-invalid")
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["error"]["type"], "RequestValidationError")
        self.assertFalse(run["error"]["retryable"])
        self.assertIsNone(run["receipt"])

    def test_concurrent_same_key_submission_creates_one_run(self) -> None:
        worker_count = 6
        key = "concurrent-request"
        payload = valid_payload()
        relays = [
            Relay(self.db_path, self.provider_path)
            for _ in range(worker_count)
        ]
        barrier = threading.Barrier(worker_count)

        def submit(relay: Relay):
            barrier.wait()
            return relay.submit(key, copy.deepcopy(payload))

        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            submissions = list(executor.map(submit, relays))

        run_ids = {submission.run_id for submission in submissions}
        self.assertEqual(len(run_ids), 1)
        self.assertEqual(
            sum(not submission.replayed for submission in submissions),
            1,
        )
        self.assertEqual(self.deployment_count(idempotency_key=key), 1)

    def test_processes_submitting_same_key_create_one_run(self) -> None:
        worker_count = 6
        context = multiprocessing.get_context("spawn")
        ready = context.Queue()
        start = context.Event()
        results = context.Queue()
        processes = [
            context.Process(
                target=submit_from_process,
                args=(
                    str(self.db_path),
                    str(self.provider_path),
                    "process-request",
                    valid_payload(),
                    ready,
                    start,
                    results,
                ),
            )
            for _ in range(worker_count)
        ]
        try:
            for process in processes:
                process.start()
            for _ in processes:
                self.assertTrue(ready.get(timeout=30))
            start.set()
            outcomes = [results.get(timeout=30) for _ in processes]
            for process in processes:
                process.join(timeout=30)
                self.assertFalse(process.is_alive())
                self.assertEqual(process.exitcode, 0)
        finally:
            start.set()
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=5)

        self.assertTrue(all(outcome[0] == "ok" for outcome in outcomes))
        self.assertEqual(len({outcome[1] for outcome in outcomes}), 1)
        self.assertEqual(sum(not outcome[2] for outcome in outcomes), 1)
        self.assertEqual(
            self.deployment_count(idempotency_key="process-request"),
            1,
        )

    def test_concurrent_same_key_different_payloads_choose_one_binding(self) -> None:
        key = "concurrent-conflict"
        payloads = [valid_payload(), valid_payload()]
        payloads[1]["assets"][0]["source_sha256"] += "-changed"
        relays = [
            Relay(self.db_path, self.provider_path)
            for _ in payloads
        ]
        barrier = threading.Barrier(len(payloads))

        def submit(index: int) -> str:
            barrier.wait()
            try:
                relays[index].submit(key, payloads[index])
            except IdempotencyConflict:
                return "conflict"
            return "created"

        with ThreadPoolExecutor(max_workers=len(payloads)) as executor:
            outcomes = list(executor.map(submit, range(len(payloads))))

        self.assertCountEqual(outcomes, ["created", "conflict"])
        self.assertEqual(self.deployment_count(idempotency_key=key), 1)

    def test_migration_rejects_historical_duplicate_keys(self) -> None:
        legacy_db = self.root / "legacy.db"
        legacy_provider = self.root / "legacy-provider.json"
        payload_json = json.dumps(valid_payload(), sort_keys=True)
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
            connection.executemany(
                """
                INSERT INTO deployments
                    (id, idempotency_key, payload_hash, payload_json, status)
                VALUES (?, 'duplicate', 'hash', ?, 'pending')
                """,
                (("run-one", payload_json), ("run-two", payload_json)),
            )

        with self.assertRaisesRegex(
            RuntimeError,
            "resolve historical duplicate deployment keys",
        ):
            Relay(legacy_db, legacy_provider)


if __name__ == "__main__":
    unittest.main()
