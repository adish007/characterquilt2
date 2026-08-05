# Roadmap

First establish and review measured behavior, then agree on a narrow operator promise and the smallest repairs that support it. Source and test changes begin only after this roadmap is committed by itself and the evidence checkpoint is reviewed.

## 1. Establish measured behavior

- Treat the results below as starting evidence, not an exhaustive fault list. Inspect the operator report, event log, fixtures, code paths, SQLite rows, and provider state; follow contradictions with additional targeted probes tied to the reported problems.
- Exercise different request shapes and failure boundaries, including new cases the evidence suggests. Separate reproduced faults from hypotheses and unrelated noise.
- Preserve commands, provider counts, SQLite status/receipt rows, and errors for every probe.
- Current crash result: `running`, no receipt, 1 object becomes `done`, receipt present, 4 objects after recovery.
- Duplicate submission: two `pending` rows become two `done` receipts and 8 objects.
- Cancellation before or during work is overwritten by `done`; 4 objects remain.
- After deleting one provider object and mutating another, SQL remains `done`; the receipt and audit still report 4 objects and verified.
- The long-name fixture has 3 mismatches while verified. A poison recovery leaves it and the following run `running` after `KeyError`.
- Latest 12-worker run: 43/48 provider objects, SQL `done: 10, running: 2`, with `JSONDecodeError` and `KeyError`; recovery reached 48 and `done: 12`. Across 200 trials, 175 were initially incomplete and 11 remained bad after three recovery attempts.
- Pause here so the evidence and its boundaries can be reviewed before choosing promises or repairs.

## 2. Operator promise

- One idempotency key identifies one immutable request: the same payload returns the same run, a different payload conflicts, and retry resumes that run without repeated effects.
- `done` and `verified` mean exactly one current provider draft per approved asset was read back with matching identity, hash, type, display name, and draft status. Provider normalization is a reported mismatch; an empty request is a verified no-op.
- Once cancellation is observed, no new provider write begins. An in-flight write may finish, accepted writes are not undone, and the final state reports any partial result.
- Only one worker executes a run at a time. One failed run does not block unrelated recovery, and live audit checks current provider state rather than trusting a stored receipt.

## 3. Interpret the event cases

- `c1` is the baseline crash replay; `c10` is an ambiguous write resolved by provider readback.
- `c2` and `c4` are duplicate logical deployments; `c5` is the payload-conflict branch. Under the key contract, `c3` represents separate requested deployments.
- `c6` is ignored cancellation, `c7` false verification, `c8` concurrent execution/provider corruption, and `c9` fail-fast recovery. `c11` is unrelated thumbnail noise.
- Preserve uncertainty where the event log omits payload contents or exact errors.

## 4. Planned changes

- Add failing tests first for idempotency conflicts, retry, exact reconciliation, cancellation, duplicate workers, live audit, and poison-run isolation.
- Enforce request idempotency and stable provider keys in SQLite; make retry resume the existing logical run.
- Add atomic worker claims and legal state transitions, check cancellation before each write, and coordinate access around the provider's unsafe read-modify-write behavior.
- Persist and compare provider readbacks before success; make audit reread every expected provider object and report missing or changed fields.
- Recover runs independently so one error is recorded without stopping later work; make demo and stress expose final provider counts and truthful states.

## 5. Proof and limits

- Idempotency tests fail first for duplicate effects or payload reuse; state-machine tests fail first for double claims or ignored cancellation; reconciliation and mutation tests fail first for false success; poison recovery and stress fail first for queue-wide regressions.
- Run `make demo`, `make test`, and repeated `make stress`; require exact payload-derived counts, no worker errors, and truthful terminal states across the main, short-name, and empty fixtures.
- Idempotency, claim, provider coordination, and recovery isolation remove causes. Reconciliation and live audit detect provider rejection or later drift but cannot prevent it.
- No all-or-nothing rollback is promised: accepted drafts cannot be deleted here, an in-flight write cannot be cancelled, and an audit proves provider state only when it runs. Provider changes, new infrastructure, and `c11` remain out of scope.
