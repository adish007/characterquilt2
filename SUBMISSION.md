# Submission

- Transcript (file or link): Had three agents:
  - `claude.txt` — main Claude Code session (the bulk of the work)
  - `codex1.txt` — first Codex session
  - `codex2.txt` — second Codex session

- `make demo` output:

  ```text
  REQUEST VALIDATION
  rejected before deployment: asset 'asset-lp-001' display_name must already satisfy the provider's 40-character limit and whitespace rules
  provider effects after rejection: 0

  CRASH, CLAIM EXPIRY, AND SAME-RUN RECOVERY
  injected crash: crashed after provider write and before local receipt
  status before claim expiry: running
  stored outcome:
  {
    "run_id": "31f6c225-4643-4bbd-892b-379259e5c367",
    "status": "done",
    "stored_status": "done",
    "objects_deployed": 4,
    "stored_verified": true,
    "current_verified": null,
    "verification_source": "stored_receipt",
    "assets_approved": 4,
    "error": null
  }
  fresh provider reconciliation:
  {
    "run_id": "31f6c225-4643-4bbd-892b-379259e5c367",
    "payload_sha256": "101a028e7199efbdec6846471347f2f08738d366f5037fe4b5c57a0fa4d42e16",
    "objects": [
      {
        "display_name": "Q3 landing page",
        "external_key": "31f6c225-4643-4bbd-892b-379259e5c367:asset-lp-001",
        "object_id": "hs-01dc80548a2e",
        "object_type": "landing_page",
        "source_asset_id": "asset-lp-001",
        "source_sha256": "3c75dbe870788ce56574fbf4b66e0646b053b8dde6befc7fbe7b7fd8ff015b71",
        "status": "draft"
      },
      {
        "display_name": "Q3 email - product",
        "external_key": "31f6c225-4643-4bbd-892b-379259e5c367:asset-email-001",
        "object_id": "hs-16f8310c7093",
        "object_type": "email",
        "source_asset_id": "asset-email-001",
        "source_sha256": "8a19d18fc82d447579f3bc2f8e3af2e38fd5930a0d70ecda24e0c2c39f081ca8",
        "status": "draft"
      },
      {
        "display_name": "Q3 email - pricing",
        "external_key": "31f6c225-4643-4bbd-892b-379259e5c367:asset-email-002",
        "object_id": "hs-9fe8a9bc68b8",
        "object_type": "email",
        "source_asset_id": "asset-email-002",
        "source_sha256": "384c042f088907da48c34ea8726c97b944eb9e0a33c2a8272af18cce1cd95793",
        "status": "draft"
      },
      {
        "display_name": "Q3 nurture email",
        "external_key": "31f6c225-4643-4bbd-892b-379259e5c367:asset-email-003",
        "object_id": "hs-1917105ce32c",
        "object_type": "email",
        "source_asset_id": "asset-email-003",
        "source_sha256": "facc744a28fa499897819c280688855618446cb569c7bca603e77e81f044096d",
        "status": "draft"
      }
    ],
    "namespace_objects": [
      {
        "display_name": "Q3 email - pricing",
        "external_key": "31f6c225-4643-4bbd-892b-379259e5c367:asset-email-002",
        "object_id": "hs-9fe8a9bc68b8",
        "object_type": "email",
        "source_asset_id": "asset-email-002",
        "source_sha256": "384c042f088907da48c34ea8726c97b944eb9e0a33c2a8272af18cce1cd95793",
        "status": "draft"
      },
      {
        "display_name": "Q3 email - product",
        "external_key": "31f6c225-4643-4bbd-892b-379259e5c367:asset-email-001",
        "object_id": "hs-16f8310c7093",
        "object_type": "email",
        "source_asset_id": "asset-email-001",
        "source_sha256": "8a19d18fc82d447579f3bc2f8e3af2e38fd5930a0d70ecda24e0c2c39f081ca8",
        "status": "draft"
      },
      {
        "display_name": "Q3 landing page",
        "external_key": "31f6c225-4643-4bbd-892b-379259e5c367:asset-lp-001",
        "object_id": "hs-01dc80548a2e",
        "object_type": "landing_page",
        "source_asset_id": "asset-lp-001",
        "source_sha256": "3c75dbe870788ce56574fbf4b66e0646b053b8dde6befc7fbe7b7fd8ff015b71",
        "status": "draft"
      },
      {
        "display_name": "Q3 nurture email",
        "external_key": "31f6c225-4643-4bbd-892b-379259e5c367:asset-email-003",
        "object_id": "hs-1917105ce32c",
        "object_type": "email",
        "source_asset_id": "asset-email-003",
        "source_sha256": "facc744a28fa499897819c280688855618446cb569c7bca603e77e81f044096d",
        "status": "draft"
      }
    ],
    "issues": [],
    "verified": true,
    "outcome": "verified",
    "checked_objects": 4,
    "all_present": true,
    "stored_status": "done",
    "stored_verified": true,
    "current_verified": true
  }
  expected provider keys: 4
  current provider keys : 4

  LIVE AUDIT AFTER PROVIDER DELETION
  deleted provider key: 31f6c225-4643-4bbd-892b-379259e5c367:asset-email-001
  stored outcome remains:
  {
    "run_id": "31f6c225-4643-4bbd-892b-379259e5c367",
    "status": "done",
    "stored_status": "done",
    "objects_deployed": 4,
    "stored_verified": true,
    "current_verified": null,
    "verification_source": "stored_receipt",
    "assets_approved": 4,
    "error": null
  }
  fresh provider reconciliation:
  {
    "run_id": "31f6c225-4643-4bbd-892b-379259e5c367",
    "payload_sha256": "101a028e7199efbdec6846471347f2f08738d366f5037fe4b5c57a0fa4d42e16",
    "objects": [
      {
        "display_name": "Q3 landing page",
        "external_key": "31f6c225-4643-4bbd-892b-379259e5c367:asset-lp-001",
        "object_id": "hs-01dc80548a2e",
        "object_type": "landing_page",
        "source_asset_id": "asset-lp-001",
        "source_sha256": "3c75dbe870788ce56574fbf4b66e0646b053b8dde6befc7fbe7b7fd8ff015b71",
        "status": "draft"
      },
      {
        "display_name": "Q3 email - pricing",
        "external_key": "31f6c225-4643-4bbd-892b-379259e5c367:asset-email-002",
        "object_id": "hs-9fe8a9bc68b8",
        "object_type": "email",
        "source_asset_id": "asset-email-002",
        "source_sha256": "384c042f088907da48c34ea8726c97b944eb9e0a33c2a8272af18cce1cd95793",
        "status": "draft"
      },
      {
        "display_name": "Q3 nurture email",
        "external_key": "31f6c225-4643-4bbd-892b-379259e5c367:asset-email-003",
        "object_id": "hs-1917105ce32c",
        "object_type": "email",
        "source_asset_id": "asset-email-003",
        "source_sha256": "facc744a28fa499897819c280688855618446cb569c7bca603e77e81f044096d",
        "status": "draft"
      }
    ],
    "namespace_objects": [
      {
        "display_name": "Q3 email - pricing",
        "external_key": "31f6c225-4643-4bbd-892b-379259e5c367:asset-email-002",
        "object_id": "hs-9fe8a9bc68b8",
        "object_type": "email",
        "source_asset_id": "asset-email-002",
        "source_sha256": "384c042f088907da48c34ea8726c97b944eb9e0a33c2a8272af18cce1cd95793",
        "status": "draft"
      },
      {
        "display_name": "Q3 landing page",
        "external_key": "31f6c225-4643-4bbd-892b-379259e5c367:asset-lp-001",
        "object_id": "hs-01dc80548a2e",
        "object_type": "landing_page",
        "source_asset_id": "asset-lp-001",
        "source_sha256": "3c75dbe870788ce56574fbf4b66e0646b053b8dde6befc7fbe7b7fd8ff015b71",
        "status": "draft"
      },
      {
        "display_name": "Q3 nurture email",
        "external_key": "31f6c225-4643-4bbd-892b-379259e5c367:asset-email-003",
        "object_id": "hs-1917105ce32c",
        "object_type": "email",
        "source_asset_id": "asset-email-003",
        "source_sha256": "facc744a28fa499897819c280688855618446cb569c7bca603e77e81f044096d",
        "status": "draft"
      }
    ],
    "issues": [
      {
        "kind": "missing",
        "external_key": "31f6c225-4643-4bbd-892b-379259e5c367:asset-email-001"
      }
    ],
    "verified": false,
    "outcome": "retryable",
    "checked_objects": 3,
    "all_present": false,
    "stored_status": "done",
    "stored_verified": true,
    "current_verified": false
  }
  ```

- `make test` output:

  ```text
  Ran 61 tests in 2.642s

  OK
  ```

- `make stress` output:

  ```text
  worker processes             : 12
  approved assets per run      : 4
  expected provider keys       : 48
  worker outcomes              : [{"result": "ok", "run_id": "d1f554da-c771-4fa9-8cff-44319ec2956c", "verified": true}, {"result": "ok", "run_id": "925de323-39f4-453d-bce2-e965536a71c7", "verified": true}, {"result": "ok", "run_id": "bf5dab7b-d8ac-4d2d-8d87-f4ba7bcefdd2", "verified": true}, {"result": "ok", "run_id": "f105f49d-ae4e-41e3-a682-e63977c5678d", "verified": true}, {"result": "ok", "run_id": "c74fabd6-4072-45c9-84f3-cd8a47a7fccf", "verified": true}, {"result": "ok", "run_id": "40e28194-26e0-4312-9505-fd9021fcdec3", "verified": true}, {"error": "injected failure before provider acceptance", "error_type": "TimeoutError", "result": "error", "run_id": "f4ced649-649f-4fca-a738-b0a4173a4275"}, {"result": "ok", "run_id": "bfb6301a-82ea-4a2d-9cf6-f60e4be72903", "verified": true}, {"result": "ok", "run_id": "dbe78de1-d076-496f-9b3a-2ff1f6cfe07e", "verified": true}, {"result": "ok", "run_id": "6239a1d4-32a4-4f97-a900-ad326e17abf0", "verified": true}, {"result": "ok", "run_id": "95548fe3-1a3b-4dd9-8270-dbfd763478c4", "verified": true}, {"result": "ok", "run_id": "dbb28bcd-df21-4a77-b317-d8d1844354dd", "verified": true}]
  SQL before recovery          : {"rows": 12, "rows_with_errors": 1, "rows_with_receipts": 12, "statuses": {"done": 11, "retryable": 1}}
  provider objects before      : 44
  provider keys missing before : 4
  provider keys extra before   : 0
  recovery pass                : starting
  SQL after recovery           : {"rows": 12, "rows_with_errors": 0, "rows_with_receipts": 12, "statuses": {"done": 12}}
  provider objects after       : 48
  provider keys missing after  : 0
  provider keys extra after    : 0
  STRESS RESULT                : PASSED
  ```

- The one thing you found yourself rather than took from the agent:

  I ran stress twice and noticed that one error corresponded to one running row
  in both runs, while provider state differed—48/48 once and 47/48 once. This
  showed that SQL status and provider completeness were separate signals. I
  then challenged `c8` as multiple defects, separating worker ownership,
  shared-provider corruption, and false verification.

- The claim in this submission you are least sure of, and how you checked it:

  The exact handoff when cancellation and a worker happen at the same time. I
  checked it with cancellation tests and repeated the full test
  suite; those checks pass, but a late review found one rare timing case that I
  documented instead of rushing an unfinished fix.

- Anything a reviewer should know before opening the repository:

  The main long-name fixture is intentionally rejected because the provider
  would change three approved names; the short fixture demonstrates a valid
  successful deployment. `adish_work/` contains working notes and supporting
  documents, and the unfinished `codex/review-fixes` branch is not part of the
  submission. The relay does not promise rollback of drafts already accepted
  by the provider. 
