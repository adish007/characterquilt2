# Submission

- Transcript: this Codex session; investigation details are retained in `adish_work/` and `Event_Interpretation.md`.
- `make demo`: exit 0. Main fixture rejected with zero effects; crashed run stayed `running` before lease expiry, recovered to four exact keys, then live audit reported `current_verified: false` and the exact deleted key while the stored receipt remained verified.
- `make test`: 56/56 passed in 3.095 seconds with `ResourceWarning` treated as an error.
- `make stress`: exit 0 with 12 spawned workers. Before recovery: 11 `done`, one visible `retryable`, 44/48 keys, one error record. After recovery: 12 `done`, 48/48 exact keys, zero error records, no extras; `STRESS RESULT: PASSED`.
- Mutation spot-check: 2/2 applied mutations were caught—skipping request validation failed the main-fixture zero-write test, and bypassing every provider gate failed the deterministic multiprocess overlap test.
- Least-certain claim: the relay prevents its own provider corruption. It is supported by a fresh-descriptor POSIX lock, deterministic overlap instrumentation of reads and writes, exact multiprocess key accounting, and crash-release testing; it does not cover non-cooperating writers or a provider crash during persistence.
- Reviewer note: completion reconciliation is sequential, not an atomic provider snapshot. Cancellation does not undo accepted writes. Provider object IDs are checked for usable uniqueness but have no request-defined expected value.
