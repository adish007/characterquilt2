# CharacterQuilt technical screen: deploy a campaign safely

Read [TASK.md](TASK.md) — it has the whole assignment.

## Setup

Python 3.11 or newer. Nothing to install. SQLite comes with Python, and the
HubSpot client here is a local fake, so there is no network call and no
credential to configure.

## Commands

```bash
make demo   # deploy the approved assets, crash partway, restart
make test   # run the visible test
```

## Files

- `TASK.md` — the assignment.
- `fixtures/deployment_request.json` — the approved landing page and emails.
- `fixtures/operator_report.txt` — what operators reported.
- `fixtures/deployment_events.jsonl` — events recorded during real runs.
- `relay/core.py` — the starter implementation and the fake HubSpot client.
- `tests/test_visible.py` — one visible test, not a full specification.
- `demo.py` — what `make demo` runs.
- `DECISIONS.md`, `SUBMISSION.md` — fill these in before you send the packet
  back.
