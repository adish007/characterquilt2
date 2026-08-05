# Agent 1 Findings: Step 1 Evidence Report

Step 1 is complete. Three agents investigated independently, and the primary agent reran their key probes. No repository source or test files were changed.

## Reproduction commands

```sh
make demo
make test
make stress

PYTHONPATH=. python /tmp/lifecycle_probe.py
PYTHONPATH=. python /tmp/same_run_race.py
PYTHONPATH=. python /tmp/concurrency_probe.py sweep --trials 20
PYTHONPATH=. python /tmp/concurrency_probe.py deep --trials 200
PYTHONPATH=. python /tmp/concurrency_probe.py hunt --trials 500
PYTHONPATH=. python /tmp/concurrency_probe.py same --trials 100
PYTHONPATH=. python /tmp/provider_race.py --trials 100
PYTHONPATH=. python /private/tmp/relay_verification_probe.py
```

The harnesses are temporary and outside the repository.

## 1. SQL lifecycle states

| Scenario | SQLite state | Receipt | Provider |
|---|---|---:|---:|
| Submitted | `pending` | no | 0 |
| Crash after first write | `running` | no | 1 |
| Restart/recovery | `done` | yes, 4 | 4 |
| Ambiguous accepted timeout | `running` | no | 1 |
| Recovery after timeout | `done` | yes, 4 | 4 |
| Cancel before execution | `cancelled` | no | 0 |
| Execute cancelled run | `done` | yes, 4 | 4 |
| Cancel during execution | briefly `cancelled`, finally `done` | yes, 4 | 4 |
| Cancel after completion | `cancelled` | yes, 4 | 4 |
| Missing/mutated provider data | `done` | yes, 4 | inconsistent or unreadable |

There is no failed/unknown state, worker owner, lease, attempt record, or per-object progress. `RunCancelled` exists but is never raised.

## 2. Submission and retry counts

| Probe | SQL result | Provider count |
|---|---|---:|
| Same key and payload submitted twice | 2 `pending`, then 2 `done` | 8 |
| Same key with a changed payload | accepted as a third run | 12 |
| Completed run retried once | 2 `done` receipts | 8 |
| Retried twice | 3 `done` receipts | 12 |
| Stuck run retried | original `running`, retry `done` | 5 |
| Recover original afterward | both `done` | 8 |

Same-run replay is safe because its provider keys remain stable. Logical-request replay is unsafe because provider keys contain the newly generated run ID.

## 3. Cancellation behavior

Cancellation is only an unconditional SQL update:

- A cancelled run can be executed normally.
- Cancellation after the first write does not stop the remaining three writes.
- Cancelling a completed run relabels it `cancelled` while preserving its receipt and four objects.
- A partial run cancelled after a crash remains one provider object with no receipt; recovery ignores it.
- A cancelled or completed run can be manually executed again.

This comes directly from [`cancel`](../relay/core.py#L147) and the unconditional status changes in [`run_once`](../relay/core.py#L179).

## 4. Concurrent worker results

Twenty-trial worker sweep:

| Workers | Initially exact | Initial provider state | Worker errors | Good after recovery |
|---:|---:|---|---|---:|
| 1 | 20/20 | 4/4 | none | 20/20 |
| 2 | 20/20 | 8/8 | none | 20/20 |
| 4 | 18/20 | failures had 12/16 | 1 `KeyError`, 1 `JSONDecodeError` | 20/20 |
| 8 | 7/20 | 23–32; 2 unreadable | 7 `KeyError`, 25 `JSONDecodeError` | 18/20 |
| 12 | 0/20 | 32–46; 7 unreadable | 15 `KeyError`, 98 `JSONDecodeError` | 13/20 |

The two-worker result is not a safety threshold; scheduling happened not to expose the race in that relay sweep.

A separate 200-trial, 12-worker run found:

- Initial: 12/200 exact, 167 readable but short, 21 unreadable.
- SQL across 2,400 rows: `done: 1,779`, `running: 621`.
- Errors: 474 `JSONDecodeError`, 147 `KeyError`.
- After three recovery attempts: 178/200 fully repaired.
- Remaining: 21 unreadable and one readable 47/48 state.
- Final SQL: `done: 2,199`, `running: 201`.

A 500-trial attack confirmed the durable categories:

- 423 good.
- 76 provider files remained unreadable.
- One ended with all 12 SQL rows `done`, all 12 receipts present, but only 47/48 provider objects. The missing object appeared in a receipt. Recovery returned without repairing it because it only scans `running`.

These frequencies are scheduling-dependent. The earlier 200-trial result recorded in the roadmap—175 initially incomplete and 11 bad after recovery—was also observed, but should not be treated as a stable rate.

## 5. Same-run double claiming

Across 100 two-worker trials against the same run:

- 99 normally made 8 provider creation calls for 4 assets.
- Provider idempotency reduced those to 4 unique objects.
- SQL ended `done` with one receipt.
- Two `JSONDecodeError`s occurred across the sample.
- A worker can therefore fail while another writes a successful shared receipt.

This proves the absence of an exclusive worker claim independently of duplicate provider effects.

## 6. Provider concurrency isolated from SQLite

The provider alone was tested with threads writing distinct objects:

| Writers | Expected | Observed after 100 trials |
|---:|---:|---|
| 1 | 1 | always 1 |
| 2 | 2 | always 1 |
| 4 | 4 | 1–3; never 4 |
| 8 | 8 | 1–3 or unreadable; never 8 |
| 12 | 12 | 1–4 or unreadable; never 12 |

This isolates the cause: [`FakeHubSpot`](../relay/core.py#L46) performs unlocked whole-file read-modify-write.

Predicted interleavings:

- `JSONDecodeError`: a read occurs while another thread has truncated but not finished writing the file.
- `KeyError`: a valid but stale whole-file save removes another worker’s key between `create_draft()` and `read()`.

Atomic replacement alone would prevent malformed reads but not lost updates; access must be serialized.

## 7. Verification and mutation results

Fixture reconciliation:

- Long fixture: SQL `done`, receipt 4, provider 4, verified—but 3 display names differ.
- Two distinct emails collapse to the identical 40-character provider name.
- Short fixture: exact 4/4 match.
- Empty fixture: `done`, verified, 0 objects; `all_present=true` because `all([])` is true.

After a correct short deployment, each mutation left SQL `done`, receipt 4, summary verified, and audit `all_present=true`:

| Mutation | Actual provider state |
|---|---|
| Delete one object | 3 objects |
| Change hash, name, and status | 4 objects, 3 field mismatches |
| Add unapproved object | 5 objects |
| Duplicate an approved source | 5 objects |
| Corrupt provider JSON | unreadable |

`audit()` only inspects stored receipt fields; it does not access the provider.

An additional duplicate-asset-ID request produced:

- 2 approved entries.
- 2 receipt entries.
- 1 unique receipt object ID.
- 1 provider object.
- SQL `done`; audit verified.

Whether malformed approval input belongs in repair scope remains a later decision.

## 8. Recovery isolation

With valid → poison → valid → valid insertion order:

```text
attempt 1: good=done; poison=running; later runs=running; KeyError
attempt 2: unchanged; KeyError
attempt 3: unchanged; KeyError
provider objects: 4
```

The exact `c9` payload/error is unknown, but the structural complaint is reproduced: [`recover`](../relay/core.py#L255) lets one exception abort everything after it.

## 9. Why green tests are misleading

Both visible tests still pass.

The integrity assertion:

- Performs zero assertions for an unknown source asset ID.
- Accepts duplicate identities.
- Does not check cardinality, names, type, status, external key, or current provider state.

The count assertion passed with four wholly wrong identities because it checks only `4 == 4`.

`make stress` also prints “objects the runs created” using `workers × assets`, not measured creation, and does not print the final provider count after recovery.

## Current cause predictions

1. Missing idempotency uniqueness plus run-based provider keys causes excess drafts.
2. Unconditional state transitions cause ignored cancellation and duplicate workers.
3. Unsynchronized provider access causes missing objects and corrupted JSON.
4. Unconditional receipts and receipt-only audit cause false verification.
5. Fail-fast recovery causes queue-wide blockage.
6. Current checks validate counts and self-reported receipts, not correctness.

## Evidence boundaries

- Concurrency frequencies are observations from nondeterministic samples, not stable probabilities or safe worker thresholds.
- The injected ambiguous timeout proves only the case where the provider committed before the response was lost.
- The synthetic malformed payload proves fail-fast recovery and queue blocking, but the event log does not reveal the exact `c9` payload or error.
- Same-run replay is safe only while its provider key and approved content remain unchanged and provider state stays readable.
- The duplicate-asset-ID probe establishes a missing validation boundary, but operator evidence does not yet establish that malformed approvals caused a reported incident.

Step 1 is ready for evidence review before the operator promise or final case grouping is treated as settled.
