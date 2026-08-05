from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from relay import InjectedCrash, Relay, RequestValidationError


def expected_keys(run_id: str, payload: dict[str, Any]) -> set[str]:
    return {
        f"{run_id}:{asset['asset_id']}"
        for asset in payload["assets"]
    }


def provider_keys(relay: Relay) -> set[str]:
    return {obj["external_key"] for obj in relay.provider.list_objects()}


def main() -> None:
    rejected_payload = json.loads(
        Path("fixtures/deployment_request.json").read_text()
    )
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

        print("REQUEST VALIDATION")
        try:
            relay.submit("campaign-deploy-invalid", rejected_payload)
        except RequestValidationError as error:
            print(f"rejected before deployment: {error}")
        else:
            raise RuntimeError("the invalid main fixture was accepted")
        rejected_effects = provider_keys(relay)
        print(f"provider effects after rejection: {len(rejected_effects)}")
        if rejected_effects:
            raise RuntimeError("rejected request wrote provider effects")

        run_id = relay.submit("campaign-deploy-001", payload).run_id
        expected = expected_keys(run_id, payload)

        print()
        print("CRASH, CLAIM EXPIRY, AND SAME-RUN RECOVERY")
        try:
            relay.run_once(
                run_id,
                crash_at="after_first_provider_write",
            )
        except InjectedCrash as error:
            print(f"injected crash: {error}")
        else:
            raise RuntimeError("the requested crash was not injected")

        restarted = Relay(
            db_path,
            provider_path,
            clock=lambda: now[0],
            claim_ttl_seconds=claim_ttl,
        )
        restarted.recover()
        active_status = restarted.get(run_id)["status"]
        print(f"status before claim expiry: {active_status}")
        if active_status != "running":
            raise RuntimeError("recovery stole an active claim")

        now[0] += claim_ttl
        restarted.recover()
        recovered = restarted.get(run_id)
        recovered_audit = restarted.audit(run_id)
        current_keys = provider_keys(restarted)
        print("stored outcome:")
        print(json.dumps(restarted.deployment_summary(run_id), indent=2))
        print("fresh provider reconciliation:")
        print(json.dumps(recovered_audit, indent=2))
        print(f"expected provider keys: {len(expected)}")
        print(f"current provider keys : {len(current_keys)}")
        if recovered["status"] != "done":
            raise RuntimeError("expired run did not recover to done")
        if not recovered_audit["verified"] or recovered_audit["issues"]:
            raise RuntimeError("recovered run did not reconcile exactly")
        if current_keys != expected:
            raise RuntimeError("recovery produced the wrong provider keys")

        # Destination drift happens outside the relay. Remove one key derived
        # from the request, then prove a new audit reads live state rather than
        # trusting the still-verified stored receipt.
        deleted_key = sorted(expected)[0]
        stored = json.loads(provider_path.read_text())
        del stored[deleted_key]
        provider_path.write_text(json.dumps(stored))

        after = Relay(db_path, provider_path)
        drift_audit = after.audit(run_id)
        missing_keys = {
            issue.get("external_key")
            for issue in drift_audit["issues"]
            if issue.get("kind") == "missing"
        }
        print()
        print("LIVE AUDIT AFTER PROVIDER DELETION")
        print(f"deleted provider key: {deleted_key}")
        print("stored outcome remains:")
        print(json.dumps(after.deployment_summary(run_id), indent=2))
        print("fresh provider reconciliation:")
        print(json.dumps(drift_audit, indent=2))
        if drift_audit["verified"]:
            raise RuntimeError("live audit trusted a stale verified receipt")
        if deleted_key not in missing_keys:
            raise RuntimeError("live audit did not identify the deleted key")


if __name__ == "__main__":
    main()
