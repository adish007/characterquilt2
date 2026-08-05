from __future__ import annotations

import json
import tempfile
from pathlib import Path

from relay import InjectedCrash, Relay, RequestValidationError


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
            print(f"REJECTED BEFORE DEPLOYMENT: {error}")
        else:
            raise RuntimeError("expected the main fixture to be rejected")
        print(
            "objects the destination is holding after rejection: "
            f"{len(relay.provider.list_objects())}"
        )

        run_id = relay.submit("campaign-deploy-001", payload).run_id

        print()
        print("VALID REQUEST CRASH AND RECOVERY")
        try:
            relay.run_once(
                run_id,
                crash_at="after_first_provider_write",
            )
        except InjectedCrash as error:
            print(f"INJECTED CRASH: {error}")

        restarted = Relay(
            db_path,
            provider_path,
            clock=lambda: now[0],
            claim_ttl_seconds=claim_ttl,
        )
        restarted.recover()
        print(
            "status while the crashed worker's claim is active: "
            f"{restarted.get(run_id)['status']}"
        )
        now[0] += claim_ttl
        restarted.recover()
        print("DEPLOYMENT SUMMARY")
        print(json.dumps(restarted.deployment_summary(run_id), indent=2))
        print("OPERATOR PRESSED CHECK AGAIN")
        print(json.dumps(restarted.audit(run_id), indent=2))
        print(
            "objects the destination is holding: "
            f"{len(restarted.provider.list_objects())}"
        )

        # Simulate destination drift after the verified deployment.
        stored = json.loads(provider_path.read_text())
        for key in list(stored)[:2]:
            del stored[key]
        provider_path.write_text(json.dumps(stored))

        after = Relay(db_path, provider_path)
        print()
        print("SAME RUN, AFTER THE DESTINATION LOST TWO OBJECTS")
        print(json.dumps(after.deployment_summary(run_id), indent=2))
        print("OPERATOR PRESSED CHECK AGAIN")
        print(json.dumps(after.audit(run_id), indent=2))
        print(
            "objects the destination is holding: "
            f"{len(after.provider.list_objects())}"
        )

        print()
        print("FINAL STATE")
        print(json.dumps(after.get(run_id), indent=2))


if __name__ == "__main__":
    main()
