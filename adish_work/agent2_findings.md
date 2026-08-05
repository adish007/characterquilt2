# Agent 2 Findings: Step 1 Measured Behavior

## Scope and method

This report covers only Roadmap Step 1: establish measured behavior before choosing promises or repairs. Three independent agents investigated the recorded events and fixtures, state-machine behavior, and thread/process concurrency. The primary agent reproduced baseline and additional operator-facing behavior and reconciled the results.

All runtime probes used `tempfile.TemporaryDirectory` or the temporary directories already used by the repository commands. No source, test, demo, stress, fixture, or roadmap file was changed during the investigation. The raw transcript preserves the full commands and outputs.

Repository baseline:

- Python: `3.12.12`.
- Starter commit: `35cb35f`.
- Roadmap-only commit: `56d5176`.
- The roadmap was committed before any source or test change.
- `PYTHONDONTWRITEBYTECODE=1 make test` passed both visible tests.
- `PYTHONDONTWRITEBYTECODE=1 make demo` completed, but reproduced a false live audit after provider objects were removed.

## Executive finding

The starter does not provide a trustworthy deployment result. Its green demo and tests cover one safe same-run crash replay, while independent faults allow duplicate effects, ignored cancellation, concurrent execution, lost or corrupt provider state, false verification, stale audit results, and recovery starvation.

The failures are not one bug. They fall into at least five directly supported families:

1. Missing request-level idempotency.
2. Unrestricted state transitions and no exclusive worker claim.
3. Uncoordinated access to provider state.
4. Receipt-only reporting without exact or live reconciliation.
5. Recovery that ignores pending work and stops on the first per-run error.

Input identity and validation expose additional reproducible failures, but the evidence does not yet establish all of them as causes of the historical incidents.

## Operator report and recorded event cases

The operator reports four symptom families:

- More drafts than approved.
- Fewer drafts than the verified receipt reports.
- A cancelled campaign continuing to completion.
- One stuck run apparently preventing later campaigns from deploying.

### Cautious case mapping

| Case | Directly recorded fact | Interpretation boundary |
|---|---|---|
| `c1` | One provider write, process crash without a local receipt, then recovery reports `done` with four provider objects. | This is the baseline same-run replay that the starter handles in isolation. |
| `c2` | The same idempotency key and payload version create two run IDs and eight provider objects. | Direct duplicate-effects evidence. |
| `c3` | Distinct keys with the same payload version create eight provider objects. | The log does not say whether both requests were intentional, so this is contract-dependent rather than proven good or bad. |
| `c4` | A stalled run is retried under the same key with a new run ID; eight objects result. | Groups with duplicate logical deployment effects. |
| `c5` | The same key is accepted for payload versions A and B. | No resulting status, receipt, or object count is recorded. Calling this a conflict depends on the chosen idempotency contract. |
| `c6` | Cancellation follows one accepted write, but the final status is `done` with four provider objects. | Direct ignored-cancellation evidence. |
| `c7` | A receipt says verified with four objects; the operator says the drafts were not what was approved. | The log does not identify the differing fields. Display-name normalization is reproduced locally but is not proven to be the historical cause. |
| `c8` | Two workers claim the same run; the receipt says verified with four objects, but the provider holds three. | Double claim and false success are direct facts. A stale-write/lost-update schedule is reproduced below. |
| `c9` | The same recovery errors three times; two later runs deploy nothing. | The exact error, payload, later statuses, and provider state are absent. |
| `c10` | A gateway timeout leaves provider acceptance ambiguous; readback finds the object. | This is an ambiguous provider outcome, distinct from the process crash in `c1`. |
| `c11` | `thumbnail_render` reports a transient timeout. | Strongest unrelated-noise candidate: that tool is absent from the deployment path and operator report. |

Important evidence limits:

- Payload versions A, B, and C cannot be mapped to the named fixture files.
- The event log has no timestamps or complete payloads.
- The exact c7 differences and c9 error are not recorded.
- Distinct keys in c3 do not prove whether the two deployments were intentional.
- The event log establishes symptoms and boundaries, not every root cause.

## Fixture measurements

| Fixture | SQL/receipt result | Provider result | Integrity result |
|---|---|---|---|
| Main request | `done`, four receipt objects, `verified: true` | Four objects | Three display-name mismatches |
| Short-name request | `done`, four receipt objects, `verified: true` | Four objects | No field mismatches |
| Empty request | `done`, zero receipt objects, `verified: true` | Zero objects | Evidence does not decide whether this is a valid no-op or invalid input |

The provider stores display names as `strip()[:40]`. For the main fixture:

- The landing-page name is truncated.
- Email 002 is truncated.
- Email 003 is truncated.
- Emails 002 and 003 collapse to the identical stored name: `Summer 2026 ABM campaign email - product`.

The main and short fixtures reuse all four asset IDs and source hashes but use different display names. Any future stable provider identity must therefore preserve deployment scope and enforce the intended request-key/payload contract; asset ID alone is not sufficient globally.

The empty fixture never reaches the injected `after_first_provider_write` crash point because there is no provider write.

## Baseline command results

### Visible tests

Command:

```text
PYTHONDONTWRITEBYTECODE=1 make test
```

Result: both tests passed. They establish that one injected crash can replay to four objects and that returned hashes match source hashes when a source ID match is found. They do not establish request idempotency, exact cardinality, unique identities, exact names, cancellation, live audit, failure isolation, or concurrency safety.

### Demo

Command:

```text
PYTHONDONTWRITEBYTECODE=1 make demo
```

Measured sequence:

1. The injected crash left one provider object and a local `running` row without a receipt.
2. Recovery reached `done`, four receipt objects, and four provider objects.
3. The demo removed two provider objects.
4. `deployment_summary()` still reported four deployed and verified.
5. `audit()` still reported four checked, `all_present: true`, and `verified: true` while the provider held two.

### Stress

One Step 1 `make stress` run reported:

- Expected objects: 48.
- Provider objects after concurrent work: 44.
- SQL statuses: `done: 10`, `running: 2`.
- Worker errors: one `JSONDecodeError` and one `KeyError`.
- One recovery pass moved all 12 rows to `done` in that sample.

The roadmap's earlier sample produced 43/48 objects with `done: 10`, `running: 2`; repeated trials below show why exact natural-schedule results vary.

## State-machine probes

### Submission and retry idempotency

- Submitting the same key and same payload twice returned distinct run IDs.
- Submitting the same key with the short-name payload returned another distinct run ID with a different payload hash.
- Executing all three runs produced three `done` receipts and 12 provider objects.
- No submission-level conflict occurred.
- Retrying a completed run created a fresh `pending` row with the same key and hash. Executing it produced two `done` receipts and eight provider objects.
- Crashing an original run after its first write and using admin retry produced a completed retry plus the still-running original. Provider count was five before recovery; recovering the original produced two completed logical copies and eight provider objects.

The database schema has only the primary-key index on `deployments.id`; there is no uniqueness rule on `idempotency_key`, no legal-status constraint, and no per-asset checkpoint table.

### Crash and ambiguous provider outcomes

Baseline injected crash:

- Local status: `running`.
- Local receipt: none.
- Provider count: one.
- Summary: zero objects deployed, not verified.
- Audit: zero checked, `all_present: true`, not verified.

Recovery replayed stable same-run `run_id:asset_id` provider keys and reached four provider objects without same-run duplicates. A second recovery and an explicit `run_once()` on the completed run did not add unique objects in isolation.

Accepted-write timeout simulation:

1. A wrapper called the real provider write successfully.
2. The wrapper then raised `TimeoutError` to simulate a lost response.
3. The row remained `running` without a receipt while the provider held one object.
4. The starter did not perform immediate provider readback.
5. A later normal recovery replayed the stable same-run key and reached `done` with four objects.

Thus eventual replay handles this transient injected case, but the direct readback behavior recorded in c10 is absent from `run_once()`.

### Cancellation and legal transitions

- Cancelling before execution produced `cancelled`, but `run_once()` unconditionally changed it to `running` and then `done`, with four provider objects.
- Cancelling immediately after the first provider write made SQL briefly say `cancelled`; the existing loop continued and finally wrote `done`, with four objects.
- Cancelling an already completed run changed its status from `done` to `cancelled` while retaining the verified four-object receipt.
- Calling `run_once()` on that cancelled run changed it back to `done`.
- A forced stale-worker schedule allowed a worker that had read `pending` to resume after another worker completed the run, change `done` back to `running`, and crash. The final row was `running` while retaining the earlier verified receipt and four provider objects.

The status field therefore does not represent a guarded state machine.

### Receipt, summary, and live audit

After a partial crash, the provider held one object but the summary reported zero. Because `audit()` computes `all()` over an empty receipt list, it returned `all_present: true` with zero checked objects.

After a completed run, the following external drift was injected:

- One provider object was deleted.
- Another object's hash, type, name, and status were changed.

Measured result:

- SQL remained `done`.
- The stored receipt remained four objects and verified.
- Provider count was three.
- `audit()` still returned four checked, `all_present: true`, and verified.

Even after the provider JSON was made unreadable, `provider.list_objects()` raised `JSONDecodeError` while `audit()` continued reporting all objects present and verified. `audit()` does not perform a provider read; it rechecks only fields already stored in the receipt.

### Recovery coverage and isolation

- A normal `pending` run remained pending after `recover()` and created no provider objects. Recovery selects only `running` rows.
- A targeted poison run missing `display_name` failed with `KeyError` after an earlier asset write and remained `running` without a receipt.
- A following valid run was put into a recoverable `running` state by the injected crash.
- Three recovery attempts each stopped on the poison run's same `KeyError`.
- The later recoverable run was never attempted, and another pending run remained pending.

This reproduces the c9 failure-isolation symptom. It does not prove that c9's historical payload C was malformed or that its exact error was `KeyError`.

### Duplicate asset identity

Two newly explored request shapes produced additional exact one-to-one failures:

- Two identical assets sharing one `asset_id` completed as `done` and verified. The receipt contained two entries with the same provider object ID, the summary reported two deployed, and the provider held one object.
- Two assets sharing one `asset_id` but differing in display name raised `IdempotencyConflict`, leaving the run `running` without a receipt and the provider holding one object.

Duplicate asset IDs are not present in the supplied fixtures or recorded events, so this is a reproduced latent fault rather than a claimed cause of c8. It is nevertheless directly relevant to any promise of exactly one provider object per approved asset.

## Concurrency measurements

### Distinct runs, threads

Forty trials ran 12 workers against distinct runs sharing one database and provider:

- Initial provider state was complete in 2/40 trials.
- It was readable but incomplete in 26/40.
- It was invalid JSON in 12/40.
- Only 1/40 trials was fully healthy when provider state, SQL states, and worker errors were considered together.
- Workers raised 193 errors: 169 `JSONDecodeError` and 24 `KeyError`.
- Four rows marked `done` across three trials were missing their current provider objects.
- After three recovery attempts, 15/40 trials remained bad: 12 had corrupt provider files and three retained four false-`done` rows.
- Only 28/40 trials reached all SQL rows `done` after those recovery attempts.

### Distinct runs, processes

Twelve trials used 12 forked worker processes:

- 0/12 were initially complete.
- Four provider files were invalid JSON.
- Eight were readable but incomplete.
- Workers raised 63 errors: 58 `JSONDecodeError` and five `KeyError`.
- Three recovery attempts repaired the eight readable trials.
- The four corrupt-file trials remained bad.

This establishes that the failure family is process-level, matching the assignment's operating assumption; it is not an artifact limited to Python threads.

### Multiple workers on the same run

Thirty same-run trials used 12 threads per trial:

- 337/360 `run_once()` calls returned successfully.
- 23 calls raised `JSONDecodeError`.
- Every final row nevertheless said `done` and the provider held four objects.

A successful sibling worker can therefore mask other workers' errors.

Controlled thread and two-process claim probes paused all workers at their first provider call. In both versions, multiple workers simultaneously reached provider work for the same run while SQL said only `running`. Each worker made all four provider-create calls and returned. Stable same-run provider keys limited the final unique provider count to four, but did not prevent duplicated work or establish exclusive ownership.

### Exact c8 schedule

A controlled schedule used the real provider load/save behavior:

1. Worker B captured a stale three-object snapshot.
2. Worker A completed four objects and wrote a verified four-object receipt.
3. Worker B overwrote provider state with its stale three-object snapshot and stopped.

Final result:

- SQL status: `done`.
- Receipt: four objects, `verified: true`.
- Provider: three objects.
- `recover()` did nothing because the row was already `done`.

This proves the c8 result is reachable through the current code. It does not measure how often that exact schedule occurs in production.

## Proven implementation mechanisms

The runtime evidence matches these source behaviors:

- `relay/core.py:46-50`: the provider client performs unsynchronized whole-file JSON reads and writes. Parallel snapshots lose updates, reads can observe truncated content, and overlapping writes can create invalid JSON.
- `relay/core.py:104-111`: the schema has no idempotency-key uniqueness rule, state-transition constraint, ownership token, failure field, or per-asset progress.
- `relay/core.py:115-129`: every submission creates a new UUID and row.
- `relay/core.py:147-152`: cancellation is an unconditional status update.
- `relay/core.py:154-177`: retry creates another run ID while keeping the original idempotency key.
- `relay/core.py:185-190`: every caller uses a previously read snapshot and unconditionally writes `running`; there is no atomic claim.
- `relay/core.py:192-203`: execution never rechecks cancellation between assets.
- `relay/core.py:205-219`: `verified` is unconditional, and `done` is written without ownership or a legal-transition predicate.
- `relay/core.py:222-237`: summaries count the stored receipt rather than current provider effects.
- `relay/core.py:239-253`: audit rereads the stored receipt, never the provider; `all([])` yields the misleading zero-object `all_present: true` result.
- `relay/core.py:261-266`: recovery selects only `running` rows and has no per-run exception isolation.
- `stress.py:34-52`: the supplied stress command exercises threads; the custom process probe establishes the same failure family under multiple processes.

No SQLite corruption or lock error was observed. The SQLite defects established here are logical: missing uniqueness, claims, guarded transitions, progress, and error isolation.

## Additional reproduced behavior and scope boundaries

The following behaviors are real but require a scope decision before repair:

- Missing required asset fields are accepted by `submit()` and fail only during provider work, leaving `running` rows.
- Duplicate asset IDs can produce false verified cardinality or provider conflicts.
- Unsupported destination and `mode: published` values are ignored; the relay still creates HubSpot drafts and reports verified.
- Empty input currently completes as a verified no-op.

The unsupported destination/mode behavior is not directly tied to the operator report. `TASK.md` says not to repair unrelated behavior, so it should remain out of scope unless the operator promise is deliberately broadened.

Other boundaries:

- Natural concurrency trial rates are sample-specific; the underlying race mechanisms and controlled schedules are what the evidence proves.
- Stable same-run provider keys prevented extra unique objects in isolated replay and same-run concurrency probes. They do not provide request-level idempotency across new run IDs.
- A process-local thread lock would not satisfy the stated multi-process requirement.
- Provider drift after completion cannot be prevented by the relay; the current fault is that it is not detected.
- Accepted provider writes cannot be assumed reversible because the supplied provider has no delete operation.
- The investigation did not establish the exact historical cause of c7 or c9.

## Evidence checkpoint

Step 1 supports the following conclusions without selecting an implementation:

- Green demo/test output is not evidence of trustworthy operator status.
- Extra drafts are directly explained by request/retry identity using new run IDs.
- Ignored cancellation is directly reproduced before, during, and after execution.
- Fewer provider objects with a verified receipt is reachable through concurrent stale writes and duplicate asset identity.
- Current verification is unconditional and current audit is not live.
- A single poison run can stop recovery of later running work, while pending work is ignored entirely.
- Multiple workers can execute one run concurrently in both threads and processes.
- Additional input and identity faults exist, but not all should automatically enter repair scope.

Before Step 2 or implementation, the evidence leaves these decisions for review:

1. What exact request-level idempotency contract should distinct and repeated keys express?
2. Must stored display names exactly match approval, or can normalization be disclosed without claiming exact verification?
3. Is an empty request a valid verified no-op or rejected input?
4. What does acknowledged cancellation promise about a provider call already in flight?
5. Should duplicate asset identity be rejected as necessary to support exact one-to-one verification?
6. Which exploratory input-validation gaps are tied closely enough to the operator report to enter scope?

No source or test repair has begun. The investigation pauses here for review.
