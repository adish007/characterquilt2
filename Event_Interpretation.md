# Event Interpretation — `c1`…`c11`

Roadmap step 3. The event log labels eleven cases and says nothing else. This file decides which
share a cause, which are noise, and — the part that governs step 4 — **which cases authorize which
repair**. `TASK.md`: *"Don't repair behavior you can't tie to the operator's report."* A repair with
no case in its authorizing column is out of scope until something authorizes it.

Evidence base: `adish_work/agent{1,2,3}_findings.md` plus the probes cited inline. Every number here
came from a command that was run; commands live in agent3 §1–§9 and the scratchpad scripts.

Three discipline rules used throughout:

1. **Recorded fact vs inference.** The log has no timestamps, no payload contents, and no error
   strings. Anything beyond the literal JSON lines is labelled inference.
2. **Reproducing a shape is not proving a history.** We can show the code *can* produce a case. Only
   where a case is deterministic does that become "this is what happened."
3. **A case that a cluster cannot mechanically produce does not belong to that cluster**, however
   well it fits the story. This rule changed our reading of `c8` (below).

---

## 1. The operator's four complaints, mapped

`fixtures/operator_report.txt` names four symptoms. Every case must land against one of them or be
argued as noise.

| Operator's words | Cases | Cluster |
|---|---|---|
| "Sometimes there are more drafts than we approved" | c2, c3, c4, (c5) | **B** |
| "Once there were fewer, and the receipt still said verified" | c7, c8 | **A + D + E** |
| "A campaign someone cancelled kept going" | c6 | **C** |
| "A stuck run seems to take the whole queue down… two campaigns behind it never went out" | c9 | **F** |
| — no operator complaint — | c1, c10, c11 | baseline / noise |

---

## 2. Cause clusters

| | Root cause (`relay/core.py`) | Cases |
|---|---|---|
| **A** | Provider state is an unsynchronised whole-file read-modify-write — `:46-50`, `:61-79` | c8 |
| **B** | Run identity is a fresh `uuid4`; the operator's key is write-only — `:117`, `:161`, `:194`, `:104-113` | c2, c3, c4, c5 |
| **C** | No state machine; `run_once` never consults `status` — `:185-190`, `:211-219` | c6 |
| **D** | `verified` is a literal; audit re-reads its own receipt — `:209`, `:222-253` | c7, c8, (c1) |
| **E** | Provider rewrites what you send; nobody compares readback to approval — `:52-53` + missing check at `:203` | c7 |
| **F** | Recovery has no isolation, no terminal failure state, wrong scope — `:262-266` | c9 |

---

## 3. Case by case

### c1 — baseline crash replay. **Healthy, and misleading.**

*Recorded:* submit `deploy-101`; one `provider_write`; `process_crash` with `local_receipt: null`;
`recover` → `status: done`, `provider_object_count: 4`.

*Verdict:* the code genuinely does this, deterministically. It works because `external_key =
f"{run_id}:{asset_id}"` (`:194`) is stable across replays of the **same** run, so re-running is
idempotent. Confirmed: re-executing one run 20/20 trials yields exactly `A` objects.

*Why it still matters.* This is the only path `make demo` exercises, and its cleanliness is the
reason the operator was told the system is fine. It is evidence about **one** failure mode
(same-run replay) and is routinely read as evidence about all of them. It also carries a Cluster D
defect that this case cannot reveal: the `done` and `verified: true` it reports are asserted, not
reconciled — identical output is produced when the destination holds nothing at all.

*Authorizes:* no repair. **Authorizes a regression test**: same-run replay must remain idempotent.
This matters because the Cluster B fix re-keys `external_key` away from `run_id`, which can break
the one thing that currently works. c1 is the guard on that.

---

### c2 — same key, twice. **Confirmed, deterministic.** Cluster B.

*Recorded:* two `submit` events, both `idempotency_key: deploy-202`, both `payload_version: A`,
different run ids; `provider_object_count: 8`.

*Reproduced exactly:* `submit()` mints a fresh `uuid4` unconditionally and never reads
`idempotency_key` back; there is no UNIQUE index on it. N submits of one key → N runs → N×A objects
(1/2/3/5 submits → 4/8/12/20 objects). 8 for A=4 is the direct, deterministic consequence.

*Authorizes:* UNIQUE index on `idempotency_key` + insert-or-return-existing. Must be a database
constraint, not SELECT-then-INSERT — 8 threads through a barrier produced 8 rows, 20/20.

---

### c3 — two different keys, identical payload. **Not a defect. Under-served.** Cluster B (boundary).

*Recorded:* `deploy-207a`/run-7 and `deploy-207b`/run-8, both `payload_version: A`;
`provider_object_count: 8`.

*Reproduced:* both accepted, identical `payload_hash`, 8 objects.

*Interpretation.* The key is the operator's unit of intent. Two keys are two requests, and payload
equality is not intent equality — deploying one approved bundle twice is a legitimate workflow.
`contract.md:10` rules exactly this. So c3 is **correct behaviour**, not a bug.

*Where we disagree with leaving it there.* c3 produces the operator's literal complaint — more
drafts than approved — and under the contract it stays silent forever. After the c2/c4/c5 repairs
ship, an operator can still see 8 drafts for 4 approved assets with no way to learn why.

*Recoverability, which is what settles it:* over-creation is visible and deletable; refusing a
legitimate second campaign is invisible to us and blocks real work. So do not block c3.

*Authorizes:* **a disclosure, not a repair.** Flag a submission whose `payload_hash` matches a prior
run carrying a *different* key, and surface the earlier run. Nothing irreversible; the operator
decides.

The query has to group by `payload_hash`, not by `idempotency_key` — grouping by key finds c2 and is
blind to c3 by construction, since c3's whole shape is *different* keys:

```
GROUP BY idempotency_key  HAVING COUNT(*)>1                    -> [('deploy-202', 2)]   c2 only
GROUP BY payload_hash     HAVING COUNT(DISTINCT idem_key)>1    -> [('2ad2ddaf', 3)]     finds c3
```

Both columns already exist in the schema.

---

### c4 — stalled run, admin retry. **Confirmed, deterministic.** Cluster B (+ F).

*Recorded:* submit `deploy-404`/run-9; `worker_stall` with `local_status: running`;
`operator_action: retry_from_admin_panel`, same key, run-10; `provider_object_count: 8`.

*Reproduced:* `retry()` mints a new `uuid4` (`:161`) while keeping the operator's key, so every
`external_key` lands in a **new provider namespace**. Retry chains 1/2/3/5/10 → 8/12/16/24/44
objects. This is the cleanest statement of Cluster B, and it is the distinction the operator cannot
see: *re-executing* a run is idempotent (always A objects), *retrying* it is not (A more every time).

*Second, independent defect in the same case.* `retry()` inserts the new row as `pending`, and
`recover()` selects `WHERE status='running'` only — so nothing ever drives it. Measured: after
submit → recover → retry → recover, statuses `{'pending': 2}`, provider 0. Meanwhile the original
row stays `running` forever. A stalled run plus the retry button can leave **two** rows that no
restart will ever pick up.

*Authorizes:* retry resumes the same logical run and provider namespace (`contract.md:36`); and
recovery scope must include `pending` (`contract.md:50`). Two separate repairs, one case.

---

### c5 — same key, two payload versions. **Mechanism confirmed; outcome not recorded.** Cluster B.

*Recorded:* `deploy-303` with `payload_version: A` (run-4) and `payload_version: B` (run-5).
**That is the entire case.** No status, no receipt, no `operator_observation`, no object count.

*Reproduced:* both accepted silently, distinct `payload_hash`, no conflict raised; when both are
executed, 8 objects and two `verified: true` receipts.

*Honest boundary.* This is the only complaint-linked case with **no recorded outcome at all**.
Calling it "the payload-conflict branch" is a statement about the contract we are choosing, not
about something the log shows. What the log *does* show — one key admitting two payload versions —
is enough, because under `contract.md:7` one key is one immutable request.

*Why it is nonetheless the most serious of the four in Cluster B.* c2/c3/c4 over-create: extra
drafts are visible and deletable. c5 lets **two different versions of the same approved campaign**
exist under one key, and no later reader can tell which one the operator approved. It is the only
member of the cluster whose failure direction is unrecoverable.

*Authorizes:* reject a repeated key with a differing `payload_hash`, before any provider write.

---

### c6 — cancelled campaign completed. **Confirmed, deterministic.** Cluster C.

*Recorded:* `provider_write` run-6; `cancel` run-6 with `local_status: cancelled`;
`operator_observation: final_local_status: done, provider_object_count: 4`.

*Reproduced, 20/20 trials:* `cancel()` is a bare UPDATE (`:147-152`). `run_once` never re-reads
status, and neither its opening UPDATE (`:185-190`) nor its terminal UPDATE (`:211-219`) has a
`WHERE status` guard. The cancelled run writes **all A** objects and the terminal UPDATE overwrites
`cancelled` with `done`. Both numbers the operator recorded — `done` and 4 — fall out exactly.

*Broader than the case.* `run_once` will drive a row in **any** state: `pending→done`,
`running→done`, `done→done`, `cancelled→done`, `failed→done`, 5/5. And `RunCancelled` is declared at
`:19` and exported, with zero raise sites. The cancellation feature was never implemented.

*Authorizes:* guarded conditional UPDATEs with rowcount checks, **and permission to begin a provider
call coordinated atomically with cancellation acknowledgement** — the same process-safe gate or
state transition that Cluster A's serialisation uses.

A bare status re-read before each write is **not sufficient**, and saying it is would ship a symptom
fix described as a cause fix. The re-read leaves a window:

1. the worker reads "not cancelled";
2. cancellation is durably recorded and acknowledged to the operator;
3. the worker begins the provider call.

`contract.md:44` promises that after cancellation is durably recorded and acknowledged, **no new
provider call begins** — which requires the check and the begin-write to be one indivisible step,
not two. Guarded terminal UPDATEs are still required on top, to stop the terminal write overwriting
`cancelled` with `done`.

Note what this case does **not** authorize: `recover()` re-reading status does not fix it (25/25
still `done`); the guard has to live in `run_once`.

---

### c7 — "receipt said verified; the drafts were not what we approved." **Mechanism confirmed; historical attribution inferred.** Clusters E + D.

*Recorded:* `provider_write` run-11; `receipt` `verified: true, objects: 4`; operator note.
The log does not say **which** fields differed. That gap is filled by the fixture, not the log.

The mechanism below is deterministic and needs no crash and no concurrency. What it is **not** is a
proof of history: the log records only that the drafts were wrong, so attributing c7 to display-name
normalisation is an inference from the fixture, well-supported but not recorded.

*Reproduced with no crash, no concurrency and no injected damage* — a single clean run of the
shipped main fixture:

- 3 of 4 approved display names are stored differently (provider `strip()[:40]`, `:52-53`);
- two distinct approved emails collapse to the **same** stored name, `"Summer 2026 ABM campaign
  email - product"` — the operator sees two indistinguishable drafts;
- the receipt says `verified: true` and audit says `all_present: true`.

*Two independent causes in one case, and they need different repairs.*
**E** is the drift: the destination rewrote what we sent. **D** is the lie: `"verified": True` is a
literal at `:209`, and `:203` records the provider's *mutated* value into the receipt — so the
receipt is self-consistent **by construction** and can never disagree with itself. Fixing E without
D leaves the false claim; fixing D without E is what actually catches it.

*The fixture trap.* `deployment_request_short.json` shows **0/4** mismatches. Any rule validated
against that fixture looks completely correct. This is `TASK.md`'s warning about fixture shapes,
made concrete.

*Authorizes:* readback reconciliation against the approved asset, and `verified` meaning
*reconciled*. It does **not** by itself authorize pre-write rejection of long names — see the note
at the end.

---

### c8 — receipt says 4, destination holds 3. **Two confirmed facts; the link between them is not established.** Clusters A + D.

*Recorded:* `worker_claim` run-14 by `w-a`; `worker_claim` run-14 by `w-b`; `provider_write` by
`w-a` for `asset-email-002`; `provider_write` by `w-b` for `asset-email-003`; `receipt verified:
true, objects: 4`; `operator_observation: provider_object_count: 3`.

This is the case the roadmap currently reads as one thing ("concurrent execution/provider
corruption"). Measurement splits it into **three** facts, and one popular link between them is
false.

**Fact 1 — no exclusive claim. Confirmed.** Two workers hold run-14 simultaneously. The code has no
claim, no lease, no ownership token; `:185-190` writes `running` unconditionally and never checks
rowcount. Real defect.

**Fact 2 — objects are missing, and the receipt says otherwise. Confirmed.** Cluster A drops
objects, Cluster D asserts success anyway. Under multi-run concurrency we measured runs ending
`done` + `verified: true` + `all_present: true` with objects permanently absent, unhealable because
`recover()` only scans `status='running'`.

**Fact 3 — the mechanism that loses the object is unsafe provider access, not the double claim
itself.** A double claim is neither sufficient nor necessary for the shortfall.

*Not sufficient, when both workers finish.* Two workers on the same run both call `run_once`, which
writes **all** assets. Identical key sets means the union is complete:

```
TWO workers, SAME run, both writing ALL assets to completion, 200 trials, A=4:
  final provider object count -> {4: 200}        <- never short
```

*But sufficient when one of them stops.* If a worker captures a stale snapshot, saves it after the
other has committed, and then stalls or dies, the redundancy that healed the case above never
happens:

```
TWO workers, SAME run; B saves a stale snapshot after A completes, then stops; 30 trials:
  (provider_held, receipt_objects, verified) -> {(1, 4, True): 30}
```

Every trial is c8's shape: the receipt claims `A` objects and says `verified: true`, the destination
holds fewer. So a same-run double claim **can** produce c8, and the earlier `{4: 200}` result says
only that it does not do so when both workers run to completion. The log never says both completed.

*Not necessary either.* The same shortfall arises with no double claim at all, from writes by a
*different* run interleaving — measured under multi-run concurrency.

*What follows.* A claim removes the same-run version of this schedule. It does **not** remove the
cross-run version, because different runs still share one provider. Serialising provider access
(Cluster A) removes both. And the log, having no timestamps, cannot establish which interleaving
actually occurred historically — we can show the code produces the shape, not that this is how it
happened.

*Consequence for step 4:* both repairs are authorized, but they are not interchangeable, and the
claim must not be credited with the whole of c8. Ship the claim alone and the cross-run loss
survives.

*Authorizes:* (i) **process-safe serialisation around provider operations** — the multi-process point
matters, since `stress.py` is thread-only and threads understate it (30% stranded vs 22%). This is
the repair that removes both torn reads and lost updates, and it goes **around** the provider, not
inside it: `TASK.md` says the provider is not ours to change. Note that atomic replacement of the
state file would be both a modification of the provider *and* insufficient — it stops tearing and
leaves lost updates untouched. (ii) live audit against current provider state; (iii) durable claim
+ fencing, authorized by Fact 1 alone, which additionally removes the same-run schedule above.

---

### c9 — one stuck run took the queue down. **Confirmed structurally; the specific trigger is unknown.** Cluster F.

*Recorded:* submit `deploy-505`, `payload_version: C`, run-15; three `recover` events, each
`outcome: error`; note that run-16 and run-17 "were submitted after run-15 and never deployed
anything."

*Reproduced:* `recover()` (`:262-266`) is `SELECT … WHERE status='running'` with **no `ORDER BY`**
followed by a bare `for row in rows: self.run_once(...)` with **no try/except**. The first row that
raises aborts the entire pass. Deterministic — identical failure on every attempt, forever:

```
poison FIRST  -> {'running': 3}            provider 0   (2 healthy runs deployed nothing)
poison MIDDLE -> {'done':1,'running':2}    provider 4
poison LAST   -> {'done':2,'running':1}    provider 8
```

The blast radius is *arrival order* and nothing else, and since the `SELECT` has no `ORDER BY`, any
reasoning about position rests on an accident of rowid order.

*What we do not know.* `payload_version: "C"` is never defined anywhere, and the log records no
error string. We deliberately do **not** claim the trigger. What we established instead is stronger
and payload-agnostic: **any** deterministically-failing row does this. We reached it three ways —
a payload missing `assets`, duplicate `asset_id`s raising `IdempotencyConflict`, and an asset
missing `display_name`.

*Two natural triggers worth citing over the constructed ones.* First, a corrupt provider state file
(Cluster A feeding Cluster F): measured 13/60 trials at W=24, after which `recover()` raises the
identical `JSONDecodeError` forever and 16 of 24 rows sit stranded. Second, **provider-side drift** —
a human editing one draft in HubSpot — after which replay raises `IdempotencyConflict` on every
attempt, 3/3, while `audit()` still reports `{'all_present': True, 'verified': True}`.

*Correction on that second trigger, which we earlier overstated.* An edited draft does **not**
automatically poison recovery. `recover()` scans `status='running'` only, so a completed run is
never revisited:

```
row status: done  ->  recover() returned cleanly (done rows are NOT scanned)
```

The earlier demonstration forced rows to `running` first, which is a precondition, not a
consequence. Drift becomes poison only when the row is reopened or re-executed — by the retry
button, by a claim expiring, or by the edit landing *before* a replaying run completes. That is a
narrower and more honest claim, and it is still a real one: every path that reopens a completed run
is a path into permanent `IdempotencyConflict`.

*Second, independent mechanism inside the same complaint.* "Never deployed anything" also happens
with no poison at all: `recover()` never drives a `pending` row, so a crash between submit and first
pickup strands a campaign forever. Whether run-16/run-17 were blocked or merely `pending` is not
recoverable from the log — but **both** mechanisms produce the operator's sentence, and both need
fixing.

*Authorizes:* per-row exception isolation; a terminal `failed` state with an attempt counter (a row
that cannot succeed must stop being retried forever); recovery scope covering `pending`.

---

### c10 — gateway timeout, readback found the object. **Not an incident; it records behaviour the starter lacks.**

*Recorded:* `provider_write` run-12 `asset-email-002` with `accepted: null`, `error:
gateway_timeout`, `retryable: true`; then `provider_readback` `found: true`.

*Interpretation.* The write landed and the response was lost; a readback resolved it. No operator
complaint attaches to this case — nothing was duplicated and nothing was lost. The outcome was good.

*But the starter cannot do what this case records.* `run_once` reads back only on the success path
(`:203`); if `create_draft` raises, the exception propagates and no readback is attempted. Measured,
with a provider that commits the write and then raises `gateway_timeout`:

```
run_once raised TimeoutError; no readback attempted
provider actually holds: 1        <- the write DID land
SQL status: running | receipt: None
```

The row sits in `running`, the receipt is absent, and the accepted write is invisible: not reported
as success, not reported as unknown, not reconciled by anything. The readback in c10's log came from
outside this code path.

*So c10 authorizes real work* — not because something broke, but because the recorded good behaviour
is behaviour the relay does not have:

- a **stable effect identity**, so a repeat of an ambiguous call cannot land as a second draft;
- a **readback after an ambiguous response**, to resolve whether the write was accepted;
- a **visible retryable/unknown outcome** when readback cannot resolve it (`contract.md:28`);
- **no blind retry under a new identity**.

*Sequencing is the safety condition.* This must land **after** request idempotency (c2/c4/c5), or a
retry added under un-repaired Cluster B amplifies the operator's loudest complaint — turning a
non-incident into duplicate drafts. That ordering is the reason this case is easy to mis-scope in
either direction: "do not repair" understates it, and repairing it first would be actively harmful.

---

### c11 — `thumbnail_render` transient timeout. **Noise. This is the planted case.**

*Recorded:* `tool_error`, run-13, `tool: thumbnail_render`, `code: transient_timeout`,
`retryable: true`. One line. No `submit`, no `provider_write`, no `operator_observation`.

*Argument.* The relay has **no tool layer**. Nothing in `relay/core.py` can emit a `tool_error`, and
`thumbnail_render` appears nowhere in the repository. It matches none of the operator's four
complaints. It is marked `retryable: true` and has no recorded consequence. `TASK.md` states at
least one case is unrelated to anything the operator reported; this is it.

*Honest limit:* this is a **structural** argument, not a measurement. We cannot prove a negative. We
can only say that no code path in this repository produces it and no operator sentence refers to it.

*Authorizes:* nothing. Identifying it and saying so is the deliverable.

---

## 4. Which cases authorize which repair

The governing table for step 4. A repair with an empty authorizing column does not ship.

| Repair | Authorized by | Cause or symptom |
|---|---|---|
| UNIQUE index on `idempotency_key`; insert-or-return-existing | c2 | cause |
| Reject repeated key with differing `payload_hash` | c5 | cause |
| `retry()` resumes the same run and provider namespace | c4 | cause |
| Recovery scope covers `pending` | c4, c9 | cause |
| Guarded UPDATEs + begin-write coordinated atomically with cancel-ack | c6 | cause |
| Process-safe serialisation **around** provider operations | c8 (Fact 3), c9 (corrupt-file trigger) | cause — removes torn reads *and* lost updates |
| Readback reconciliation; `verified` means *reconciled* | c7, c8 | cause |
| Live audit against current provider state | c7, c8 | **symptom detection** — finds drift, cannot prevent it |
| Per-row isolation in `recover()` + terminal `failed` + attempt counter | c9 | cause |
| Durable claim + fencing | c8 (Fact 1) | cause of the same-run schedule only — **not** of cross-run loss |
| Readback after an ambiguous response + retryable/unknown outcome | c10 | cause — **sequence after** c2/c4/c5 |
| Duplicate-payload disclosure across different keys (group by `payload_hash`) | c3 | disclosure only |
| Same-run replay idempotence regression test | c1 | guard on the c2/c4 repair |

**Deliberate non-repairs, with reasons:**

| Not doing | Why |
|---|---|
| Anything for c11 | No tool layer exists; unrelated to every operator complaint |
| Blocking c3 | Correct behaviour under `contract.md:10`; blocking fails unrecoverably |
| Modifying the provider's storage (tmp-file + `os.replace`) | `TASK.md`: the provider is not ours to change. Also insufficient — it stops tearing and leaves lost updates |
| A claim as the *whole* fix for missing drafts | It removes only the same-run schedule; different runs still share one provider |
| A blind retry on ambiguous responses | Safe only after c2/c4/c5; before that it amplifies Cluster B |
| WAL / SQLite tuning | `busy_timeout` is already 5000; `OperationalError` count across every trial: **0** |
| Validating `destination` / `mode: "published"` | Real (agent2), tied to no operator complaint |
| Canonical-JSON key ordering, `str()` coercion, unicode graphemes, `object_id` collision math | Real and deterministic; tied to no case and no complaint |

---

## 5. What stays uncertain

1. **c5 has no recorded outcome** — no status, receipt, or count. We repair it on the strength of the
   contract, not on evidence of harm.
2. **c8's historical interleaving is unknowable.** Both a same-run schedule (where one worker saves a
   stale snapshot and stops) and cross-run interference reproduce the shape; with no timestamps the
   log cannot say which occurred, or whether both `worker_claim` lines and the shortfall are even the
   same episode. We report both defects, authorize both repairs, and decline to claim a history.
3. **c9's trigger is unknown.** `payload_version: "C"` is undefined and no error string was recorded.
   We claim the payload-agnostic mechanism and cite the natural triggers, not the constructed probe.
4. **c9's "never deployed anything" has two sufficient mechanisms** — poison-blocking and
   `pending`-stranding. The log cannot say which applied to run-16/run-17.
5. **c7's differing fields are not in the log.** Display-name truncation is reproduced from the
   fixture and matches the complaint exactly, but the log does not name it.
6. **c11's noise verdict is structural, not measured.** We cannot prove the absence of a cause.
7. **c3's intent is unknowable.** Two keys may be one operator's mistake or two campaigns. We
   surface rather than decide, precisely because we cannot know.
8. **Every concurrency rate here is machine- and load-dependent** and the three investigations
   disagree by up to 3× on the corruption rate. Direction is solid; no point estimate should travel
   without its N.
9. **Cluster A's mechanism does not generalise to real HubSpot** — it is a property of a JSON file on
   local disk. Its *symptom* (destination holds fewer objects than the receipt claims) does, and
   Clusters B–F generalise as-is.

---

## 6. One case that does not authorize what the contract does with it

`contract.md:14-15` rejects a request pre-write when a display name would be normalised, and states
that the supplied main campaign is therefore rejected.

c7 authorizes **detection** — the operator's complaint is that they were not told. It does not
authorize **rejection**, which is a stronger action producing zero drafts where the operator
previously got four imperfect ones. Worth recording because the cost is concrete: `demo.py:12`,
`stress.py:20` and both visible tests all load `fixtures/deployment_request.json`, so under that
clause every shipped entrypoint operates on a payload that never writes — and `make stress`, the
harness for the Cluster A guarantee, would exercise zero provider writes.

That is a legitimate product decision and it is now stated in the contract rather than hidden. It is
recorded here as a place where the repair is broader than the case that authorizes it.
