# Decisions

Short notes are fine. Fill this in before you submit.

- Time actually spent: 2 hours 30 minutes
- What changed between your roadmap and what you shipped:
The roadmap had some issues like duplicated deployments, ignored cancellation, corrupted provider state under concurrency, falsely verified missing or changed objects, and let failed runs disrupt recovery. I shipped request validation and idempotency, same-run retry, fenced claims, cross-process provider locking, exact reconciliation/live audit, partial-outcome reporting, and isolated recovery. 

- What you had the coding agent do, and where you overrode it:
  I used coding agents to investigate failures, challenge the contract, implement each roadmap stage, and review new commits. I kept control of scope by preserving the evidence checkpoint, prioritizing material findings. Biggest places where I overode it was helping them figure out it was more than one bug for c8

- What your implementation actually promises an operator:
Sending the same request again returns the same run, and retry continues that run instead of creating another deployment. A run only reports success after checking that every draft matches what was approved; failures and partial results remain visible.

- For each promise, the check that fails first if it stops holding:
Request tests catch duplicate or changed submissions, worker tests catch ownership and concurrency problems, reconciliation tests catch incorrect drafts, cancellation tests catch work continuing after cancellation, and make stress catches recovery problems.

- What you fixed at the cause, and what you only stopped from showing:

I fixed the main problems which were duplicate requests, overlapping workers, unsafe provider writes, and one failed run blocking recovery. For smaller provider-side issues I could not prevent, like normalization or later drift, I made sure they are detected and shown clearly instead of being reported as successful.

- What is still unsafe, including anything that came up during the session and
  stayed open:

    The main workflow and expected failure paths are covered. A few narrow timing cases remain around claim expiry and cancellation, along with conflicting provider list/read results; the system also cannot roll back accepted provider writes or prevent changes after an audit.

- The next failure you would inject:

I would stop a worker right before it sends a draft, cancel the run or let another worker take over, and then release it. The old worker should not create anything new or change the saved result.

