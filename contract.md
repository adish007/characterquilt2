# Operator Contract

This is the behavior the deployment relay must satisfy before its implementation may be described as trustworthy. It is intentionally narrower than an all-or-nothing deployment system.

## Request identity and validation

- One idempotency key identifies one immutable deployment request.
- Validation occurs before the idempotency key is reserved. A rejected request does not bind its key, so the operator may correct the payload and resubmit with that key.
- Submitting the same key with the same canonical payload returns the same run.
- Submitting the same key with a different payload fails before any provider write.
- Different keys identify separate deployments, even when their payloads are identical. The submission response discloses prior runs with the same `payload_hash` without merging or blocking the new run.
- An explicitly supplied `assets: []` means the operator deliberately approved zero assets. It succeeds as a verified no-op with zero provider objects.
- A missing or non-list `assets` value is invalid.
- Every asset must contain usable, non-empty `asset_id`, `source_sha256`, `type`, and `display_name` values.
- Asset IDs must be unique within a request. Duplicates are rejected before any provider write.
- Approved display names must be stored exactly. Names outside provider constraints known to the relay are rejected before any provider write. Any unpredicted provider normalization is detected by readback reconciliation and prevents verified success.
- Under the fake provider's known 40-character constraint, the supplied main campaign is intentionally rejected before writing because three approved names would be changed.
- This contract covers the `hubspot-marketing` destination in `draft` mode. Other destination/mode combinations remain outside the supported contract.

## Completion and verification

- `done` and `verified` mean that each expected object matched when it was read during the successful reconciliation pass immediately preceding completion. This is not an atomic provider snapshot.
- There is exactly one provider draft for every approved asset and no unexpected draft in that deployment's provider namespace.
- Each draft matches the approved asset ID, source hash, type, exact display name, and `draft` status.
- A receipt records the provider objects actually reconciled; a count or the receipt's own assertion is not sufficient evidence.
- A run that cannot satisfy these conditions does not report `done` or `verified`.

## Failure outcomes

- **Rejected:** deterministic request validation fails before any provider write. The operator receives terminal validation details.
- **Retryable or unknown:** a crash, timeout, missing object during completion reconciliation, temporarily unreadable provider, or ambiguous provider response remains recoverable until readback or replay resolves it. The uncertainty is visible; it is not reported as success or left as unexplained `running` work.
- **Failed:** conflicting content already stored under the effect identity or a deterministic field mismatch is a terminal needs-attention failure. The operator receives the conflict and every provider effect the relay has reconciled.
- **Cancelling:** cancellation has been durably acknowledged, but at least one call already in flight has not resolved. Its uncertainty and known effects remain visible.
- **Cancelled:** every call that began before cancellation has resolved or been explicitly recorded as unresolved according to provider capability. The terminal result is unverified and reports all reconciled effects.
- Failure of one run is recorded without preventing other eligible runs from being attempted.

## Retry, interruption, and concurrency

- Provider calls may be repeated after a crash or ambiguous response, but replay uses the same effect identity and never creates a second object. Provider drift may produce a terminal conflict rather than eventual success.
- Retry resumes the same logical run and provider namespace; it does not create a second deployment.
- Only the worker holding the run's active durable claim may execute it or commit its status and receipt.
- Claims have durable ownership, expiry, renewal, and fencing semantics. A worker whose claim has expired or been replaced is fenced from beginning new provider calls and from committing later status or receipt changes.
- A provider call begun while a claim was valid may finish after claim expiry. That temporary overlap is not a second valid claim; its outcome is reconciled without allowing the stale worker to start more work or commit run state.
- The relay coordinates its own concurrent provider operations so they cannot corrupt, erase, or make unreadable another run's provider objects.
- One worker's successful completion cannot conceal another worker error that affects the run's result.

## Cancellation

- After cancellation is durably recorded and acknowledged, no new provider call begins.
- A provider call already in flight may finish, and previously accepted drafts are not removed.
- The operator view remains `cancelling` while an in-flight outcome can still be resolved. It becomes terminal `cancelled` only after the effect report includes the resolved result or explicitly records an outcome the provider cannot resolve.
- The terminal cancelled report includes every reconciled provider effect, including an in-flight write that finishes after cancellation. An unresolved ambiguous call is reported as unknown rather than omitted or claimed successful.

## Recovery and audit

- Each recovery pass considers pending work and interrupted work whose claim is no longer active. It does not steal work with an active claim.
- A stale or expired worker cannot overwrite the state produced by the worker that recovered its run.
- An error in one run does not stop the pass from considering other eligible runs.
- Audit reads current provider state. Missing, changed, duplicated, unexpected, or unreadable state fails audit with useful details.
- A successful receipt proves only the individual readbacks made during its reconciliation pass; later audit is required to detect drift.

## Explicit non-promises

- Deployment is not atomic across SQLite and the provider.
- Reconciliation is a sequence of provider reads, not an atomic provider snapshot.
- Cancellation does not roll back accepted provider writes or stop a call already in flight.
- The relay does not prevent the provider from changing or losing objects after completion.
- The relay promises idempotent effects, not exactly one provider API call.
- The relay cannot delete or otherwise clean up duplicate or partial drafts created before these guarantees were implemented.

## First checks that enforce this contract

- Idempotency tests fail first on duplicate effects or same-key payload reuse.
- Payload-disclosure tests fail first if a different key with the same `payload_hash` is merged, blocked, or returned without prior-run disclosure.
- Preflight tests fail first on missing/non-list `assets`, missing or unusable required asset fields, duplicate asset IDs, or names outside known provider constraints; all assert zero provider writes and prove the rejected key can be reused with corrected content.
- Exact reconciliation tests fail first on missing, duplicate, unexpected, or mismatched provider objects.
- Worker and multi-process provider tests fail first on simultaneous valid claims, a stale worker beginning new work, lost updates, unreadable state, an active claim being stolen, or an expired worker committing. They allow a call begun under a valid claim to finish after expiry.
- Cancellation tests fail first if a call begins after durable acknowledgement, the terminal state is overwritten, or a later in-flight effect is omitted.
- Recovery tests fail first if pending work is ignored, expired work is not recovered, an active claim is stolen, or a poison run blocks a healthy run.
- Ambiguous-response tests fail first if an accepted write is repeated under a different effect identity or if readback does not resolve its visible state.
- Failure-reporting tests fail first if validation writes anything, terminal details are absent, partial effects are omitted, or uncertainty is reported as verified.
- Mutation-based audit tests fail first when current provider drift is reported as verified.
