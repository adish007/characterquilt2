from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from relay import ClaimLost, Relay, RunCancelled


def payload(asset_count: int = 2) -> dict[str, Any]:
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


class BlockingProvider:
    def __init__(self, delegate: Any, *, block_on: str) -> None:
        self.delegate = delegate
        self.block_on = block_on
        self.entered = threading.Event()
        self.release = threading.Event()
        self.create_calls = 0

    def _block(self, operation: str) -> None:
        if operation == self.block_on and not self.entered.is_set():
            self.entered.set()
            if not self.release.wait(timeout=5):
                raise TimeoutError("test did not release provider operation")

    def create_draft(
        self,
        *,
        external_key: str,
        asset: dict[str, Any],
    ) -> dict[str, str]:
        self.create_calls += 1
        self._block("create")
        return self.delegate.create_draft(external_key=external_key, asset=asset)

    def read(self, external_key: str) -> dict[str, str]:
        result = self.delegate.read(external_key)
        self._block("read")
        return result

    def list_objects(self) -> list[dict[str, str]]:
        return self.delegate.list_objects()


class CancellationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.db_path = self.root / "relay.db"
        self.provider_path = self.root / "provider.json"
        self.relay = Relay(self.db_path, self.provider_path)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def submit(self, key: str, asset_count: int = 2) -> str:
        return self.relay.submit(key, payload(asset_count)).run_id

    def wait_for_status(self, run_id: str, status: str) -> None:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if self.relay.get(run_id)["status"] == status:
                return
            time.sleep(0.005)
        self.fail(f"run never reached {status!r}")

    def test_cancel_before_work_writes_nothing_and_is_terminal(self) -> None:
        run_id = self.submit("before")

        receipt = self.relay.cancel(run_id)

        state = self.relay.get(run_id)
        self.assertEqual(state["status"], "cancelled")
        self.assertFalse(receipt["verified"])
        self.assertEqual(receipt["outcome"], "cancelled")
        self.assertEqual(receipt["objects"], [])
        self.assertEqual(self.relay.provider.list_objects(), [])
        with self.assertRaises(RunCancelled):
            self.relay.run_once(run_id)

    def test_cancel_waits_for_inflight_write_and_reports_its_effect(self) -> None:
        run_id = self.submit("inflight")
        provider = BlockingProvider(self.relay.provider, block_on="create")
        self.relay.provider = provider

        with ThreadPoolExecutor(max_workers=2) as pool:
            worker = pool.submit(self.relay.run_once, run_id)
            self.assertTrue(provider.entered.wait(timeout=2))
            canceller = pool.submit(self.relay.cancel, run_id)
            self.wait_for_status(run_id, "cancelling")
            provider.release.set()
            with self.assertRaises(ClaimLost):
                worker.result(timeout=2)
            receipt = canceller.result(timeout=2)

        self.assertEqual(self.relay.get(run_id)["status"], "cancelled")
        self.assertEqual(len(receipt["objects"]), 1)
        self.assertEqual(provider.create_calls, 1)

    def test_acknowledged_cancel_prevents_the_next_asset_call(self) -> None:
        run_id = self.submit("between")
        provider = BlockingProvider(self.relay.provider, block_on="read")
        self.relay.provider = provider

        with ThreadPoolExecutor(max_workers=2) as pool:
            worker = pool.submit(self.relay.run_once, run_id)
            self.assertTrue(provider.entered.wait(timeout=2))
            canceller = pool.submit(self.relay.cancel, run_id)
            self.wait_for_status(run_id, "cancelling")
            provider.release.set()
            with self.assertRaises(ClaimLost):
                worker.result(timeout=2)
            receipt = canceller.result(timeout=2)

        self.assertEqual(provider.create_calls, 1)
        self.assertEqual(len(receipt["objects"]), 1)
        self.assertEqual(self.relay.get(run_id)["status"], "cancelled")

    def test_done_cannot_be_relabelled_cancelled(self) -> None:
        run_id = self.submit("done", asset_count=1)
        completed = self.relay.run_once(run_id)

        returned = self.relay.cancel(run_id)

        self.assertEqual(self.relay.get(run_id)["status"], "done")
        self.assertEqual(returned, completed)

    def test_unreadable_reconciliation_stays_cancelling_until_recovery(self) -> None:
        run_id = self.submit("unknown", asset_count=1)
        self.provider_path.write_text("{bad json")

        receipt = self.relay.cancel(run_id)

        state = self.relay.get(run_id)
        self.assertEqual(state["status"], "cancelling")
        self.assertEqual(receipt["outcome"], "unknown")
        self.assertTrue(state["error"]["retryable"])

        self.provider_path.write_text(json.dumps({}))
        self.relay.recover()
        self.assertEqual(self.relay.get(run_id)["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
