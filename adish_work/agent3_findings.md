# Agent 3 Findings — Step 1 Evidence

Research only. **No source or test file in the repo was created, edited or deleted.** All probe
scripts live outside the repo (paths below). This is the evidence a roadmap would rest on, plus
candidate groupings and a candidate issue list — not the roadmap itself.

Method: 13 agents swept six dimensions (concurrency, cancellation, mutation, idempotency, recovery,
provider fidelity), each with an adversarial verifier that re-ran its numbers; a synthesis pass
re-measured everything load-bearing; then I independently re-verified the counterintuitive claims.
Convention throughout: `A = len(payload["assets"])`, computed, never hardcoded. `W` = worker count.

Scripts: `<scratch>/mine/{sweep,corrupt,mproc,sqltables,errident,silentloss,verify}.py`
Full agent notes: `<scratch>/RESEARCH_NOTES.md` (362 lines), raw logs in `<scratch>/consolidation/`.
`<scratch>` = `/private/tmp/claude-501/-Users-jaina-Downloads-02-durable-run-relay/7db8a93c-.../scratchpad`

---

## 1. The headline: `make stress` sits on the knee of the curve

`stress.py` ships with `WORKERS = 12`. That is the mildest setting at which the defect is visible
at all, and it hides how fast things degrade. 10 trials per row:

```
workers expected   min    med   max  loss%  errors
      1        4     4    4.0     4   0.0%  -
      2        8     8    8.0     8   0.0%  -
      4       16    12   16.0    16   0.0%  KeyError 1, JSONDecodeError 1
      8       32    26   32.0    32   0.0%  JSONDecodeError 3, KeyError 4
     12       48    36   44.0    47   8.3%  KeyError 11, JSONDecodeError 8
     16       64    47   53.0    64  17.2%  JSONDecodeError 7, KeyError 19
     24       96    -1   78.5    86  18.2%  JSONDecodeError 45, KeyError 30
     32      128    92  103.5   112  19.1%  JSONDecodeError 55, KeyError 34
```
`PYTHONPATH=. python3 <scratch>/mine/sweep.py`

Two things the shipped script cannot show you:
- **Errors start at W=4, three worker-counts before the count goes visibly short.** The errors are
  the leading indicator; the count is the lagging one.
- **`-1` at W=24 means `list_objects()` itself raised.** That is a different and worse failure than
  a short count (§3).

> **Correction — do not read a safe threshold off this table.** The `0.0%` rows are a 10-trial
> artifact. At 100 trials, W=2 end-to-end already fails 2/100; and the provider *in isolation* (two
> writers entering `create_draft` together) loses an object in **98/100** trials:
> ```
> PROVIDER IN ISOLATION, 100 trials       END-TO-END via run_once, 100 trials
>   writers=1 -> {1: 100}                   workers=2 -> {8: 98, 5: 1, unreadable: 1}
>   writers=2 -> {1: 98, 2: 2}              workers=4 -> {16: 91, 15: 2, 14: 1, 13: 1, 12: 5}
>   writers=4 -> {1: 74, 2: 26}
>   writers=8 -> {2: 60, 1: 32, 3: 4, unreadable: 4}
> ```
> `PYTHONPATH=. python3 <scratch>/mine/twowriters.py`
> The race is **always** present; low worker counts merely widen the gap between the two provider
> calls enough that the SQLite work either side usually separates them. **There is no safe
> concurrency limit** — agent1's provider-isolation table is the evidence for that, not this
> end-to-end curve, which understates the race at every width.

To be precise about the first bullet: errors do start at W=4 end-to-end, but that is where the race
becomes *observable*, not where it begins.

SQL across all 990 rows in that sweep: `done/receipt=True: 772`, `running/receipt=False: 218` —
**22% of runs strand in `running` with no receipt.**

---

## 2. Your `personalnotes.txt` observation is causally exact

You noted: *"Number of errors is equal to the number of workers that are running."* I tried to break
it and could not:

```
Claim: errors_raised == rows left in 'running'
  over 200 trials across worker counts 4/8/12/16/24: {'match': 200}
```
`PYTHONPATH=. python3 <scratch>/mine/errident.py`

It is not a coincidence. `run_once` sets `status='done'` as its **last** statement
(`relay/core.py:211-219`), after the row was already committed to `running` at `:186-190`. Any
exception before that leaves the row at `running`. So *one exception ⇔ one stranded row*, exactly.

**The consequence matters more than the identity.** `errors raised` is a faithful proxy for stranded
runs — and neither it nor `run status counts` counts objects actually *lost*. A run can end `done`
with a complete receipt and still be missing provider objects (§4). The two numbers `make stress`
prints are blind to the failure the operator is complaining about.

---

## 3. The provider state file can be destroyed permanently

`FakeHubSpot._save` is `Path.write_text` (`core.py:49-50`) — open-truncate, then write. No lock,
no tmp-file + `os.replace`, no fsync. A short write landing over a longer one leaves trailing bytes
from the previous content:

```
provider state file after 24-worker run, 60 trials:
  CORRUPT-AT-REST: 13/60      readable: 47/60
  e.g. bytes=19317, tail: '...7fbe7b7fd8ff015b71","status":"draft"}}}}'   <- note the extra braces
```
`PYTHONPATH=. python3 <scratch>/mine/corrupt.py`

When it happens, **every object from every run is unreadable** — not just the racing one — and it is
absorbing: once corrupt, always corrupt. `recover()` raises the identical `JSONDecodeError` forever.
The agents measured 16 of 24 rows stranded `running` with receipt NULL in one such trial.

And the dashboard does not notice:
```
list_objects   -> RAISES JSONDecodeError
audit          -> {'checked_objects': 4, 'all_present': True, 'verified': True}
summary        -> {'objects_deployed': 4, 'verified': True}
```

Deleting the state file outright is handled even more quietly — `Relay()` recreates it as `{}`
(`core.py:43-44`), so the destination holds nothing and `audit()` still returns `all_present: true`.

**Corrected — only one fix is available, and it is sufficient.** An earlier version of this section
recommended tmp-file + `os.replace` alongside a lock. That was wrong on both counts:

- It is **out of bounds.** `_save` is inside `FakeHubSpot`. `TASK.md`: *"The provider is not yours to
  change — treat it the way you would treat HubSpot. If you need to simulate how it behaves, do that
  around it, not inside it."* Atomic publish cannot be done around the provider, only inside it.
- It is **insufficient anyway.** Atomic replacement stops the tearing and the corrupt-at-rest file,
  and removes **none** of the lost updates — those come from load-then-save being two operations, not
  from the write being non-atomic.

The repair that is both in bounds and sufficient is **process-safe serialisation around provider
operations**: if every load-modify-save runs under one inter-process gate, there is no interleaving
to tear the file *and* no stale snapshot to lose an update. It must be inter-process — a
`threading.Lock` looks correct under `make stress`, which is thread-only, and does nothing under the
multi-process assumption TASK.md sets.

---

## 4. `stress.py` understates the problem: it uses threads, not processes

TASK.md says to assume *"more than one worker process"*. `stress.py` uses `threading`, which is
GIL-serialized. True multi-process, W=12, 10 trials:

```
provider objects: min=36 med=40 max=47 (expected 48)
state file UNREADABLE: 1/10 trials
errors: JSONDecodeError 33, KeyError 3
SQL statuses: {'done': 84, 'running': 36}   <- 30% stranded
```
`PYTHONPATH=. python3 <scratch>/mine/mproc.py`

vs. threads at the same width: 22% stranded, 8 JSONDecodeErrors. Processes are meaningfully worse,
and processes are what the constraint says to assume.

### The unhealable case

The worst outcome is a run that ends **`done` + `verified` + `audit.all_present=true` with objects
permanently absent**, because `recover()` only scans `status='running'` (`core.py:262-263`) and so
never revisits it:

```
workers= 12  40 trials -> {'clean': 34, 'provider_unreadable': 4, 'SILENT_LOSS': 2}
             runs reporting done+verified while objects are MISSING: 2
             approved objects silently absent from the destination : 6
```
`PYTHONPATH=. python3 <scratch>/mine/silentloss.py` (after running `recover()` to convergence)

The agents saw `recover()` x5 return `[47,47,47,47,47]` against an expected 48 and stop. **Honest
caveat: this is racy and my 40-trial samples give no stable rate** — I saw 2/40 at W=12 and 0/40 at
W=8 and W=16, while the agents saw anywhere from ~1% to 67% depending on width and threads-vs-processes.
The *direction* is solid (worse with more workers, worse with processes); no point estimate should be
quoted without its N and the machine.

---

## 5. SQL run-status tables, per scenario

`PYTHONPATH=. python3 <scratch>/mine/sqltables.py`

```
--- S1b crash+recover (the ONLY state make demo ever shows) --- held: 4
  run       idem_key       p_hash    status   receipt
  6a1968fc  campaign-001   2ad2ddaf  done     True

--- S2 same idempotency key submitted twice (c2) --- held: 8   <- 2x approved
  abb58866  deploy-202     2ad2ddaf  done     True
  033ef3c1  deploy-202     2ad2ddaf  done     True

--- S3 retry() from admin panel (c4) --- held: 8   <- same key, 2x approved
  2f769a0e  deploy-404     2ad2ddaf  done     True
  4e134546  deploy-404     2ad2ddaf  done     True

--- S4 same key, DIFFERENT payload (c5) ---
  2d6f5152  deploy-303     2ad2ddaf  pending  False
  c3f1c72e  deploy-303     101a028e  pending  False   <- differing hash, no conflict raised

--- S5 cancel() racing a live run (c6) --- held: 4
  77eb8126  deploy-cancel  2ad2ddaf  done     True    <- cancelled -> done, objects written anyway

--- S6b poison run, after 3x recover() (c9) --- held: 1
  bdfc4714  deploy-505     71276e7a  running  False   <- IdempotencyConflict, every attempt
  abd49658  deploy-506     2ad2ddaf  running  False   <- healthy, deployed NOTHING
  8f14cdbf  deploy-507     2ad2ddaf  running  False   <- healthy, deployed NOTHING
```

Agent measurement of the same poison run at three queue positions — the blast radius is *arrival
order*, nothing else:
```
poison FIRST  -> {'running':3}              provider 0  (expected 8 from the 2 healthy runs)
poison MIDDLE -> {'done':1,'running':2}     provider 4
poison LAST   -> {'done':2,'running':1}     provider 8
```
`recover()`'s `SELECT` has no `ORDER BY` (`core.py:262-264`), so "the poison is at position k" is
resting on an accident of rowid order.

### Cancel is not advisory-ish, it is simply ignored

20/20 trials with `cancel()` fired on a separate connection right after the first provider write:
`(status='done', provider_objects=A, verified=True, all_present=True)` — every time. `run_once`
never re-reads status; neither UPDATE has a `WHERE status` guard. `run_once` will drive a row in
**any** state: `pending→done, running→done, done→done, cancelled→done, failed→done`, 5/5.

`RunCancelled` is declared at `core.py:19` and exported, with **0 raise sites and 0 except sites**.

---

## 6. Error census

| type | message form | where | scenario |
|---|---|---|---|
| `JSONDecodeError` | `Expecting value: line 1 column 1` | 119 in my sweep; 30/89/55 in agent trials | reader lands inside the truncate window; **transient**, aborts one run |
| `JSONDecodeError` | `Extra data: line 1 column N` | 13/60 at W=24 (mine); 6/20 (agents) | short write over a long one; **permanent**, file unparseable forever |
| `KeyError` | `'<run_id>:<asset_id>'` | 99 in my sweep | `provider.read()` at `:203` of an object another worker's stale save erased |
| `KeyError` | `'assets'` / `'display_name'` | 3/3 recover attempts | malformed payload `submit()` accepted without validation |
| `IdempotencyConflict` | `provider key '…' was reused` | 3/3 recover attempts | a human edits a stored draft, then the run replays (§7) |
| `TypeError`/`AttributeError` | `object of type 'NoneType' has no len()` | 5 of 25 malformed payloads | **`deployment_summary()` itself can be crashed by a run the service accepted** |
| `sqlite3.OperationalError` | `database is locked` | **0** in every trial | see §9 — do not spend budget on WAL |
| `InjectedCrash` | — | fires on main/short fixtures, **not** on empty | injection point is inside the asset loop (`:199`) |

---

## 7. Two things only reachable by driving the system somewhere the demo never goes

**A `pending` row is never driven by anything.** `recover()` selects `WHERE status='running'` only:
```
after submit :  {'pending': 1}
after recover:  {'pending': 1}  provider: 0   <- never driven
after retry():  {'pending': 2}                 <- retry() inserts ANOTHER pending row
recover again:  {'pending': 2}  provider: 0
```
A crash between `submit` and the first pickup strands a campaign forever, and the admin panel's
retry button creates exactly such a row. `audit()` on it returns `all_present: true`.

**A successful run becomes a permanent poison pill if anyone edits one draft in HubSpot.** Edit one
stored `display_name`, and every subsequent recovery pass raises `IdempotencyConflict` — identical,
3/3 — while `audit()` continues to report `{'all_present': True, 'verified': True}`. This is the
*natural* trigger for §5's head-of-line blocking; the malformed payload is a constructed probe.

---

## 8. Zero-damage drift: the strongest finding, and it needs no crash and no concurrency

The provider owns its objects and normalises them: `_store_display_name` strips and truncates to 40
chars (`core.py:39,52`). Nothing anywhere compares the readback to the approved asset, and
`"verified": True` is a **literal** at `core.py:209`. On a single clean run of the shipped main
fixture:

```
mismatches: 3 of 4 assets
  approved 'Summer 2026 ABM campaign email - product deep dive'
  stored   'Summer 2026 ABM campaign email - product'
  approved 'Summer 2026 ABM campaign email - product deep dive, part two '
  stored   'Summer 2026 ABM campaign email - product'      <- SAME as the one above
distinct stored display_names: 3 for 4 assets
receipt: verified=true    audit: all_present=true
```

Two approved emails collapse into one indistinguishable name in HubSpot. This is `c7` verbatim:
*"receipt said verified; the drafts were not what we approved."*

**This is the TASK.md trap.** `deployment_request_short.json` shows **0/4** mismatches. A rule
validated against that fixture looks completely correct. The receipt is self-consistent *by
construction* — `:203` records the provider's mutated value — so it can never disagree with itself.

The empty fixture is the mirror-image trap: `all([])` is `True`, so it reports
`{'objects_deployed': 0, 'verified': true, 'all_present': true}` — maximally green over nothing.

---

## 9. Two negative results that should stop work, not start it

These are worth as much as the defects, because each kills a plausible fix.

- **A worker lease will not fix `c8`.** Two workers driving the *same* `run_id`: provider object
  count was `A` in **20/20** of my trials and 60/60 of the agents' — zero duplicates, ever. The
  run-scoped `external_key = f"{run_id}:{asset_id}"` (`:194`) plus the equality short-circuit at
  `:72-77` already prevents same-run duplication. `c8`'s two `worker_claim` lines are real, but they
  are **not** the cause of its short count; Cluster A is. Note the corollary: any fix that re-keys
  `external_key` away from `run_id` *destroys* this accidental guarantee unless a claim is added at
  the same time.
- **SQLite is not the bottleneck.** Live pragmas: `{'journal_mode': 'delete', 'busy_timeout': 5000,
  'synchronous': 2}`. `sqlite3.OperationalError` count across every trial in this report: **0**.
  Python's default `timeout=5.0` already absorbs the contention. WAL is a non-fix.

---

## 10. Candidate cause clusters (for step 3)

Grouped by shared **root cause**, not by symptom.

| cluster | root (relay/core.py) | explains |
|---|---|---|
| **A** Provider file is an unsynchronised whole-file read-modify-write | `:46-50` load/save; `:61-79` load…mutate…save | **c8**; "once there were fewer, and the receipt still said verified" |
| **B** Run identity is a fresh uuid4; the operator's key is write-only | `:117`, `:161` uuid4; `:194` run-scoped key; `:104-113` no UNIQUE on `idempotency_key` | **c2, c3, c4, c5**; "more drafts than we approved" |
| **C** No state machine — `run_once` never consults `status` | `:185-190`, `:211-219` unguarded UPDATEs | **c6**; "a campaign someone cancelled kept going" |
| **D** `verified` is asserted; `audit()` re-reads its own receipt | `:209` literal; `:222-253` never touch `self.provider` | **c1, c7, c8**; "the receipt still said verified" |
| **E** Destination rewrites what you send; nobody compares readback to approval | `:52-53` truncation + missing comparison at `:203` | **c7** |
| **F** Recovery has no isolation, no terminal failure state, wrong scope | `:262-266` no try/except, no ORDER BY, `running`-only | **c9**; "a stuck run takes the whole queue down" |

**Noise — measured, correct, not roadmap material:**
`c11` (`thumbnail_render` — the relay has **no tool layer**; nothing in `core.py` can emit it; this is
TASK.md's "at least one is unrelated"). Also: canonical-JSON key-ordering for non-string keys,
`str()`-coercion of asset fields, unicode grapheme splitting, `object_id` birthday arithmetic, and
SQLite/WAL. All real, all deterministic, none tied to any operator line or `c` case.

**Genuinely ambiguous — flag, do not silently decide:**
- **c3** (different keys, identical payload → 8 objects). The key is the operator's unit of intent;
  payload equality is not intent equality. But over-creation is the **recoverable** direction —
  extra drafts are visible and deletable, nothing was overwritten. c5 (same key, divergent payload)
  is the unrecoverable one. Posture: don't block c3, *surface* it. One `GROUP BY idempotency_key`
  already detects it.
- **c10** (`gateway_timeout`, readback `found: true`). Either a correctly-handled transient or
  evidence there is no retry policy at all — the fixture cannot distinguish these. Being wrong by
  leaving it alone is recoverable; "fixing" it risks adding a retry that duplicates drafts under B.
- **Empty fixture `verified: true`.** Vacuous-true is the unrecoverable direction (it green-lights
  total loss). Refusing to verify an empty deployment is recoverable (an operator complains).
- **The truncation itself.** The provider is entitled to normalise. What is not defensible is calling
  the result `verified` without telling the operator what changed. Note: a naive reconcile marks
  **every** healthy run of the main fixture as drifted — that is a product decision to state out loud.

---

## 11. Candidate issue list (for step 4), by operator harm × confidence

| # | issue | now (measured) | cause or symptom | cluster |
|---|---|---|---|---|
| 1 | `audit()`/`summary()` never contact the destination | provider wiped → `all_present:true, verified:true, objects_deployed:A` | **CAUSE** of the lie; **not** of the loss — shipped alone it is a better alarm on the same fire | D |
| 2 | Concurrent writers silently drop each other's objects | 3/20 trials at shipped W=12 reach `W*A`; live stress 5/5 short | CAUSE | A |
| 3 | State file left permanently unparseable | 13/60 at W=24; 16/24 rows stranded; `recover()` fails forever | CAUSE — same repair as #2 (one gate removes tearing *and* lost updates); atomic replace is out of bounds and insufficient | A |
| 4 | `recover()` aborts the whole pass on the first raising row | poison first → 0 of 2 healthy runs deploy, deterministic 3/3 | CAUSE | F |
| 5 | Cancel does not cancel | 20/20 → `done`, all `A` objects written, audit green | CAUSE | C |
| 6 | One key → N runs → N×A drafts | 1/2/3/5 submits → 4/8/12/20 objects | CAUSE (needs a UNIQUE index, not SELECT-then-INSERT) | B |
| 7 | Same key + different payload silently forks | 2 rows, 2 hashes, 8 objects, both verified | CAUSE; the only unrecoverable direction | B |
| 8 | `retry()` forks the provider namespace and leaves a `pending` row nothing drives | keys under the new run_id; original stalls `running` forever | CAUSE | B+F |
| 9 | A green run can be missing objects, unhealable | 2/40 at W=12; `recover()` converges below `W*A` and stops | CAUSE if #2 lands; SYMPTOM suppression if only #1 does | A+D |
| 10 | Receipt never records that the destination changed what we approved | 3/4 assets renamed, 2 emails collapse to 1 name, `verified: true` | CAUSE | E |
| 11 | Nothing advances a `pending` row | `recover()` leaves it untouched; `audit()` says `all_present: true` | CAUSE | F |
| 12 | `verified: true` over zero objects | empty fixture green on every surface | CAUSE (of the vacuous truth) | D |

Deliberate non-fixes to state explicitly: WAL/SQLite locking (§9), a worker lease as the `c8` fix
(§9), and everything in the noise bucket.

---

## 12. What we still do not know

1. **Every racy rate is machine- and load-dependent, and the three sources disagree** (green-but-missing:
   ~1% to 67% depending on width and threads-vs-processes). Direction is solid; no point estimate
   should be quoted without its N and the machine.
2. **Nobody has measured the operator's literal complaint** — how often `audit()`/`summary()` disagree
   with `provider.list_objects()` across a shipped `make stress` run. That is the cheapest regression
   test available and it does not exist.
3. **No control baseline.** Nothing has been measured *with* a process-safe gate around the provider.
   The claim that one gate removes both tearing and lost updates is reasoned from the code, not proven.
4. **The fixtures cannot distinguish "never wrote" from "wrote then lost."** `recover()` re-creates
   dropped objects and rewrites a byte-identical receipt, leaving no artifact that anything was ever
   missing. `demo.py:40-45` fakes this deterministically; the natural version is indistinguishable
   from a healthy run afterwards.
5. **`c8`'s provenance is shape-matching, not proof.** Both a same-run schedule (one worker saves a
   stale snapshot and stops — measured 30/30) and cross-run interference reproduce it. With no
   timestamps the log cannot say which occurred, or whether the two `worker_claim` lines and the
   shortfall are the same episode. See `Event_Interpretation.md` §3 (c8).
6. **`c9`'s exact payload is unknown.** `payload_version: "C"` is never defined. I showed the
   *mechanism* is payload-agnostic — any deterministically-failing row blocks everyone behind it —
   which is the honest form of that claim.
7. **`c11` is asserted noise on structural grounds, not measured.** We cannot prove a negative; we can
   only note that no tool layer exists in `relay/core.py`.
8. **Concurrency was only ever driven with the main fixture.** A pool mixing empty and 4-asset
   payloads is untested by anyone.
9. **The real HubSpot is not this fake.** Cluster A's *mechanism* is a property of a JSON file on
   local disk and does not generalise. Its *symptom* — destination holds fewer objects than the
   receipt claims — certainly does. Clusters B–F generalise as-is.

---

## 13. Why the green tests prove nothing

Both visible tests pass on every state described above.

- `test_every_deployed_object_matches_its_source_asset` performs **zero** assertions when a source
  asset id doesn't match, checks no cardinality, no name, no type, no status, and never looks at
  current provider state.
- `test_reported_deployment_recovers_without_duplicate_drafts` asserts `len(list_objects()) ==
  len(payload["assets"])` — it passed in §8 with 4 objects carrying 3 distinct names and two
  indistinguishable emails. `4 == 4` is not a completeness test.
- `stress.py` prints `objects the runs created` as `WORKERS * assets` — arithmetic, not a
  measurement — and never prints the provider count *after* recovery.
