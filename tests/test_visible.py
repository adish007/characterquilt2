from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from relay import InjectedCrash, Relay


class ReportedDeploymentFailureTest(unittest.TestCase):
    def test_reported_deployment_recovers_without_duplicate_drafts(self) -> None:
        payload = json.loads(
            Path("fixtures/deployment_request_short.json").read_text()
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db_path = root / "deployments.db"
            provider_path = root / "fake-hubspot.json"
            now = [1_000.0]
            claim_ttl = 10.0
            relay = Relay(
                db_path,
                provider_path,
                clock=lambda: now[0],
                claim_ttl_seconds=claim_ttl,
            )
            run_id = relay.submit("reported-deployment", payload).run_id

            with self.assertRaises(InjectedCrash):
                relay.run_once(
                    run_id,
                    crash_at="after_first_provider_write",
                )

            now[0] += claim_ttl
            restarted = Relay(
                db_path,
                provider_path,
                clock=lambda: now[0],
                claim_ttl_seconds=claim_ttl,
            )
            restarted.recover()

            state = restarted.get(run_id)
            self.assertEqual(state["status"], "done")
            self.assertIsNotNone(state["receipt"])
            self.assertEqual(
                len(restarted.provider.list_objects()),
                len(payload["assets"]),
            )


    def test_every_deployed_object_matches_its_source_asset(self) -> None:
        payload = json.loads(
            Path("fixtures/deployment_request_short.json").read_text()
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            relay = Relay(root / "d.db", root / "p.json")
            run_id = relay.submit("integrity-check", payload).run_id
            receipt = relay.run_once(run_id)

            for stored in receipt["objects"]:
                for asset in payload["assets"]:
                    if asset["asset_id"] == stored["source_asset_id"]:
                        self.assertEqual(
                            stored["source_sha256"], asset["source_sha256"]
                        )


if __name__ == "__main__":
    unittest.main()
