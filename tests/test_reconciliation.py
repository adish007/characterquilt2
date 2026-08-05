from __future__ import annotations

import copy
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from typing import Any

from relay import IdempotencyConflict, Relay, RequestValidationError


def payload(*, asset_count: int = 2) -> dict[str, Any]:
    return {
        "destination": "hubspot-marketing",
        "mode": "draft",
        "assets": [
            {
                "asset_id": f"asset-{index}",
                "source_sha256": f"sha-{index}",
                "type": "email",
                "display_name": f"Approved asset {index}",
            }
            for index in range(asset_count)
        ],
    }


def provider_object(external_key: str, asset: dict[str, Any]) -> dict[str, str]:
    return {
        "object_id": f"object-for-{asset['asset_id']}",
        "external_key": external_key,
        "source_asset_id": str(asset["asset_id"]),
        "source_sha256": str(asset["source_sha256"]),
        "object_type": str(asset["type"]),
        "display_name": str(asset["display_name"]),
        "status": "draft",
    }


class AcceptedThenTimeoutProvider:
    """Accept a write, but make its response ambiguous to the relay."""

    def __init__(self, delegate: Any) -> None:
        self.delegate = delegate
        self.create_calls: list[str] = []
        self.read_calls: list[str] = []

    def create_draft(
        self,
        *,
        external_key: str,
        asset: dict[str, Any],
    ) -> dict[str, str]:
        self.create_calls.append(external_key)
        self.delegate.create_draft(external_key=external_key, asset=asset)
        raise TimeoutError("provider accepted the write but response was lost")

    def read(self, external_key: str) -> dict[str, str]:
        self.read_calls.append(external_key)
        return self.delegate.read(external_key)

    def list_objects(self) -> list[dict[str, str]]:
        return self.delegate.list_objects()


class MissingWriteProvider:
    """Return no accepted object and leave same-key readback missing."""

    def __init__(self, delegate: Any) -> None:
        self.delegate = delegate

    def create_draft(
        self,
        *,
        external_key: str,
        asset: dict[str, Any],
    ) -> dict[str, str]:
        return provider_object(external_key, asset)

    def read(self, external_key: str) -> dict[str, str]:
        raise KeyError(external_key)

    def list_objects(self) -> list[dict[str, str]]:
        return self.delegate.list_objects()


class TimeoutBeforeWriteProvider(MissingWriteProvider):
    def create_draft(
        self,
        *,
        external_key: str,
        asset: dict[str, Any],
    ) -> dict[str, str]:
        raise TimeoutError("provider timed out before accepting the write")


class ListedObjectsProvider:
    def __init__(self, delegate: Any, objects: list[dict[str, str]]) -> None:
        self.delegate = delegate
        self.objects = objects

    def read(self, external_key: str) -> dict[str, str]:
        return self.delegate.read(external_key)

    def list_objects(self) -> list[dict[str, str]]:
        return copy.deepcopy(self.objects)


class ReconciliationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.db_path = self.root / "deployments.db"
        self.provider_path = self.root / "provider.json"
        self.relay = Relay(self.db_path, self.provider_path)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def issue_kinds(self, report: dict[str, Any]) -> set[str]:
        return {str(issue["kind"]) for issue in report["issues"]}

    def test_short_request_is_verified_only_after_exact_readback(self) -> None:
        approved = payload()
        run_id = self.relay.submit("exact-success", approved).run_id

        receipt = self.relay.run_once(run_id)

        self.assertEqual(self.relay.get(run_id)["status"], "done")
        self.assertTrue(receipt["verified"])
        self.assertEqual(receipt["outcome"], "verified")
        self.assertEqual(receipt["issues"], [])
        self.assertEqual(len(receipt["objects"]), len(approved["assets"]))
        by_asset = {obj["source_asset_id"]: obj for obj in receipt["objects"]}
        for asset in approved["assets"]:
            stored = by_asset[asset["asset_id"]]
            self.assertEqual(stored["source_sha256"], asset["source_sha256"])
            self.assertEqual(stored["object_type"], asset["type"])
            self.assertEqual(stored["display_name"], asset["display_name"])
            self.assertEqual(stored["status"], "draft")

    def test_explicit_empty_request_is_an_exact_verified_no_op(self) -> None:
        run_id = self.relay.submit("empty-success", payload(asset_count=0)).run_id

        receipt = self.relay.run_once(run_id)
        audit = self.relay.audit(run_id)

        self.assertEqual(receipt["objects"], [])
        self.assertEqual(receipt["issues"], [])
        self.assertTrue(receipt["verified"])
        self.assertTrue(audit["verified"])
        self.assertEqual(self.relay.provider.list_objects(), [])

    def test_main_fixture_is_rejected_without_reserving_or_writing(self) -> None:
        main_payload = json.loads(
            Path("fixtures/deployment_request.json").read_text()
        )

        with self.assertRaises(RequestValidationError):
            self.relay.submit("main-preflight", main_payload)

        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            rows = connection.execute(
                "SELECT COUNT(*) FROM deployments WHERE idempotency_key = ?",
                ("main-preflight",),
            ).fetchone()[0]
        self.assertEqual(rows, 0)
        self.assertEqual(self.relay.provider.list_objects(), [])

    def test_accepted_write_timeout_is_resolved_by_same_key_readback(self) -> None:
        approved = payload(asset_count=1)
        run_id = self.relay.submit("ambiguous-accepted", approved).run_id
        provider = AcceptedThenTimeoutProvider(self.relay.provider)
        self.relay.provider = provider

        receipt = self.relay.run_once(run_id)

        expected_key = f"{run_id}:{approved['assets'][0]['asset_id']}"
        self.assertEqual(provider.create_calls, [expected_key])
        self.assertIn(expected_key, provider.read_calls)
        self.assertEqual(len(self.relay.provider.list_objects()), 1)
        self.assertEqual(self.relay.get(run_id)["status"], "done")
        self.assertTrue(receipt["verified"])
        self.assertNotIn("ambiguous_write", self.issue_kinds(receipt))

    def test_existing_conflicting_content_is_terminal_and_reports_effects(self) -> None:
        approved = payload(asset_count=1)
        run_id = self.relay.submit("provider-conflict", approved).run_id
        external_key = f"{run_id}:{approved['assets'][0]['asset_id']}"
        conflicting = provider_object(external_key, approved["assets"][0])
        conflicting["source_sha256"] = "different-content"
        self.provider_path.write_text(json.dumps({external_key: conflicting}))

        with self.assertRaises(IdempotencyConflict):
            self.relay.run_once(run_id)
        state = self.relay.get(run_id)

        self.assertEqual(state["status"], "failed")
        self.assertIsNotNone(state["error"])
        self.assertFalse(state["error"]["retryable"])
        self.assertEqual(state["receipt"]["outcome"], "failed")
        self.assertFalse(state["receipt"]["verified"])
        self.assertIn("mismatch", self.issue_kinds(state["receipt"]))
        self.assertEqual(state["receipt"]["objects"], [conflicting])

    def test_missing_readback_is_retryable_unknown_not_success(self) -> None:
        approved = payload(asset_count=1)
        run_id = self.relay.submit("missing-readback", approved).run_id
        self.relay.provider = MissingWriteProvider(self.relay.provider)

        with self.assertRaises(RuntimeError):
            self.relay.run_once(run_id)
        state = self.relay.get(run_id)

        self.assertEqual(state["status"], "retryable")
        self.assertIsNotNone(state["error"])
        self.assertTrue(state["error"]["retryable"])
        self.assertEqual(state["receipt"]["outcome"], "retryable")
        self.assertFalse(state["receipt"]["verified"])
        self.assertIn("missing", self.issue_kinds(state["receipt"]))

    def test_unreadable_provider_during_execution_is_retryable_unknown(self) -> None:
        approved = payload(asset_count=1)
        run_id = self.relay.submit("unreadable-execution", approved).run_id
        self.provider_path.write_text("{not valid json")

        with self.assertRaises(json.JSONDecodeError):
            self.relay.run_once(run_id)
        state = self.relay.get(run_id)

        self.assertEqual(state["status"], "retryable")
        self.assertIsNotNone(state["error"])
        self.assertTrue(state["error"]["retryable"])
        self.assertFalse(state["receipt"]["verified"])
        self.assertIn("provider_unreadable", self.issue_kinds(state["receipt"]))

    def test_timeout_before_write_remains_retryable_and_ambiguous(self) -> None:
        approved = payload(asset_count=1)
        run_id = self.relay.submit("ambiguous-unaccepted", approved).run_id
        self.relay.provider = TimeoutBeforeWriteProvider(self.relay.provider)

        with self.assertRaises(TimeoutError):
            self.relay.run_once(run_id)
        state = self.relay.get(run_id)

        self.assertEqual(state["status"], "retryable")
        self.assertTrue(state["error"]["retryable"])
        self.assertFalse(state["receipt"]["verified"])
        self.assertIn("ambiguous_write", self.issue_kinds(state["receipt"]))

    def test_live_audit_checks_each_promised_field(self) -> None:
        approved = payload(asset_count=1)
        run_id = self.relay.submit("typed-mutations", approved).run_id
        receipt = self.relay.run_once(run_id)
        baseline = json.loads(self.provider_path.read_text())
        external_key = next(iter(baseline))

        mutations: tuple[tuple[str, str], ...] = (
            ("external_key", f"{run_id}:changed-key"),
            ("source_asset_id", "changed-asset"),
            ("source_sha256", "changed-sha"),
            ("object_type", "changed-type"),
            ("display_name", "Changed display name"),
            ("status", "published"),
        )
        for field, changed_value in mutations:
            with self.subTest(field=field):
                damaged = copy.deepcopy(baseline)
                damaged[external_key][field] = changed_value
                self.provider_path.write_text(json.dumps(damaged))

                audit = self.relay.audit(run_id)

                self.assertFalse(audit["verified"])
                mismatches = [
                    issue
                    for issue in audit["issues"]
                    if issue["kind"] == "mismatch"
                ]
                self.assertTrue(mismatches, audit)
                self.assertIn(field, mismatches[0]["fields"])
                self.assertTrue(receipt["verified"])

    def test_object_id_checks_enforce_usable_unique_identity_and_its_limit(
        self,
    ) -> None:
        approved = payload(asset_count=1)
        run_id = self.relay.submit("object-id-limit", approved).run_id
        self.relay.run_once(run_id)
        baseline = json.loads(self.provider_path.read_text())
        external_key = next(iter(baseline))

        invalid = copy.deepcopy(baseline)
        invalid[external_key]["object_id"] = ""
        self.provider_path.write_text(json.dumps(invalid))
        invalid_audit = self.relay.audit(run_id)
        self.assertFalse(invalid_audit["verified"])
        self.assertIn("invalid_object_id", self.issue_kinds(invalid_audit))

        # The request defines no provider object ID. A different usable unique
        # value is therefore indistinguishable from a legitimate provider ID
        # reassignment; audit must not pretend it can prove otherwise.
        changed = copy.deepcopy(baseline)
        changed[external_key]["object_id"] = "different-usable-object-id"
        self.provider_path.write_text(json.dumps(changed))
        changed_audit = self.relay.audit(run_id)
        self.assertTrue(changed_audit["verified"])
        self.assertEqual(changed_audit["issues"], [])

    def test_live_audit_rejects_same_count_missing_and_unexpected(self) -> None:
        approved = payload(asset_count=2)
        run_id = self.relay.submit("same-count-drift", approved).run_id
        self.relay.run_once(run_id)
        damaged = json.loads(self.provider_path.read_text())
        removed_key = sorted(damaged).pop()
        del damaged[removed_key]
        unexpected_key = f"{run_id}:not-approved"
        damaged[unexpected_key] = provider_object(
            unexpected_key,
            {
                "asset_id": "not-approved",
                "source_sha256": "unexpected-sha",
                "type": "email",
                "display_name": "Unexpected object",
            },
        )
        self.provider_path.write_text(json.dumps(damaged))

        audit = self.relay.audit(run_id)

        self.assertEqual(
            len(audit["namespace_objects"]),
            len(approved["assets"]),
        )
        self.assertFalse(audit["verified"])
        self.assertIn("missing", self.issue_kinds(audit))
        self.assertIn("unexpected", self.issue_kinds(audit))

    def test_live_audit_detects_duplicate_external_and_object_identities(self) -> None:
        approved = payload(asset_count=2)
        run_id = self.relay.submit("duplicate-live-state", approved).run_id
        self.relay.run_once(run_id)
        baseline = self.relay.provider.list_objects()

        duplicated_key = baseline + [copy.deepcopy(baseline[0])]
        self.relay.provider = ListedObjectsProvider(
            self.relay.provider,
            duplicated_key,
        )
        external_duplicate = self.relay.audit(run_id)
        self.assertFalse(external_duplicate["verified"])
        self.assertIn("duplicate", self.issue_kinds(external_duplicate))

        duplicate_id = copy.deepcopy(baseline)
        duplicate_id[1]["object_id"] = duplicate_id[0]["object_id"]
        self.relay.provider = ListedObjectsProvider(
            self.relay.provider.delegate,
            duplicate_id,
        )
        object_duplicate = self.relay.audit(run_id)
        self.assertFalse(object_duplicate["verified"])
        self.assertIn("duplicate_object_id", self.issue_kinds(object_duplicate))

    def test_live_audit_ignores_objects_in_another_run_namespace(self) -> None:
        first = self.relay.submit("first-namespace", payload(asset_count=1)).run_id
        second = self.relay.submit(
            "second-namespace",
            payload(asset_count=1),
        ).run_id
        self.relay.run_once(first)
        self.relay.run_once(second)

        first_audit = self.relay.audit(first)
        second_audit = self.relay.audit(second)

        self.assertTrue(first_audit["verified"])
        self.assertTrue(second_audit["verified"])
        self.assertEqual(len(first_audit["objects"]), 1)
        self.assertEqual(len(second_audit["objects"]), 1)

    def test_live_audit_ignores_falsely_asserted_receipt_and_detects_drift(
        self,
    ) -> None:
        approved = payload(asset_count=2)
        run_id = self.relay.submit("live-audit", approved).run_id
        original_receipt = self.relay.run_once(run_id)
        original_state = json.loads(self.provider_path.read_text())
        expected_keys = sorted(original_state)

        false_receipt = copy.deepcopy(original_receipt)
        false_receipt["verified"] = True
        false_receipt["issues"] = []
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                "UPDATE deployments SET receipt_json = ? WHERE id = ?",
                (json.dumps(false_receipt), run_id),
            )

        damaged = copy.deepcopy(original_state)
        del damaged[expected_keys[0]]
        self.provider_path.write_text(json.dumps(damaged))
        missing = self.relay.audit(run_id)
        self.assertFalse(missing["verified"])
        self.assertIn("missing", self.issue_kinds(missing))
        self.assertEqual(self.relay.get(run_id)["status"], "done")

        damaged = copy.deepcopy(original_state)
        damaged[expected_keys[0]]["display_name"] = "provider changed this"
        self.provider_path.write_text(json.dumps(damaged))
        mismatched = self.relay.audit(run_id)
        self.assertFalse(mismatched["verified"])
        self.assertIn("mismatch", self.issue_kinds(mismatched))

        damaged = copy.deepcopy(original_state)
        unexpected_key = f"{run_id}:not-approved"
        damaged[unexpected_key] = provider_object(
            unexpected_key,
            {
                "asset_id": "not-approved",
                "source_sha256": "unexpected-sha",
                "type": "email",
                "display_name": "Unexpected object",
            },
        )
        self.provider_path.write_text(json.dumps(damaged))
        unexpected = self.relay.audit(run_id)
        self.assertFalse(unexpected["verified"])
        self.assertIn("unexpected", self.issue_kinds(unexpected))

        self.provider_path.write_text("not-json")
        unreadable = self.relay.audit(run_id)
        self.assertFalse(unreadable["verified"])
        self.assertIn("provider_unreadable", self.issue_kinds(unreadable))


if __name__ == "__main__":
    unittest.main()
