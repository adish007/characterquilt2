from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any


class InjectedCrash(RuntimeError):
    pass


class IdempotencyConflict(RuntimeError):
    pass


class RunCancelled(RuntimeError):
    pass


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _digest_text(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class FakeHubSpot:
    """Small durable provider simulation with idempotent draft creation.

    Like the real destination, this provider owns the objects it stores. It
    accepts what you send, applies its own rules to it, and returns what it
    decided to keep.
    """

    DISPLAY_NAME_LIMIT = 40

    def __init__(self, state_path: Path) -> None:
        self.state_path = Path(state_path)
        if not self.state_path.exists():
            self.state_path.write_text("{}")

    def _load(self) -> dict[str, dict[str, str]]:
        return json.loads(self.state_path.read_text())

    def _save(self, objects: dict[str, dict[str, str]]) -> None:
        self.state_path.write_text(_canonical_json(objects))

    def _store_display_name(self, value: str) -> str:
        return str(value).strip()[: self.DISPLAY_NAME_LIMIT]

    def create_draft(
        self,
        *,
        external_key: str,
        asset: dict[str, Any],
    ) -> dict[str, str]:
        objects = self._load()
        existing = objects.get(external_key)
        candidate = {
            "object_id": f"hs-{_digest_text(external_key)[:12]}",
            "external_key": external_key,
            "source_asset_id": str(asset["asset_id"]),
            "source_sha256": str(asset["source_sha256"]),
            "object_type": str(asset["type"]),
            "display_name": self._store_display_name(asset["display_name"]),
            "status": "draft",
        }
        if existing is not None:
            if existing != candidate:
                raise IdempotencyConflict(
                    f"provider key {external_key!r} was reused"
                )
            return dict(existing)
        objects[external_key] = candidate
        self._save(objects)
        return dict(candidate)

    def read(self, external_key: str) -> dict[str, str]:
        return dict(self._load()[external_key])

    def list_objects(self) -> list[dict[str, str]]:
        return [dict(value) for value in self._load().values()]


class Relay:
    def __init__(self, db_path: Path, provider_state_path: Path) -> None:
        self.db_path = Path(db_path)
        self.provider = FakeHubSpot(provider_state_path)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_db(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS deployments (
                    id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    receipt_json TEXT
                )
                """
            )

    def submit(self, idempotency_key: str, payload: dict[str, Any]) -> str:
        """Starter behavior: incorrectly creates a new deployment every time."""
        run_id = str(uuid.uuid4())
        payload_json = _canonical_json(payload)
        payload_hash = _digest_text(payload_json)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO deployments
                    (id, idempotency_key, payload_hash, payload_json, status)
                VALUES (?, ?, ?, ?, 'pending')
                """,
                (run_id, idempotency_key, payload_hash, payload_json),
            )
        return run_id

    def get(self, run_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM deployments WHERE id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            raise KeyError(run_id)
        result = dict(row)
        result["payload"] = json.loads(result.pop("payload_json"))
        receipt_json = result.pop("receipt_json")
        result["receipt"] = (
            json.loads(receipt_json) if receipt_json is not None else None
        )
        return result

    def cancel(self, run_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE deployments SET status = 'cancelled' WHERE id = ?",
                (run_id,),
            )

    def retry(self, run_id: str) -> str:
        """The admin panel's retry button for a deployment operators call stuck.

        Starter behavior: re-runs the same approved request under a fresh
        deployment, keeping the operator's original idempotency key.
        """
        previous = self.get(run_id)
        new_run_id = str(uuid.uuid4())
        payload_json = _canonical_json(previous["payload"])
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO deployments
                    (id, idempotency_key, payload_hash, payload_json, status)
                VALUES (?, ?, ?, ?, 'pending')
                """,
                (
                    new_run_id,
                    str(previous["idempotency_key"]),
                    str(previous["payload_hash"]),
                    payload_json,
                ),
            )
        return new_run_id

    def run_once(
        self,
        run_id: str,
        *,
        crash_at: str | None = None,
    ) -> dict[str, Any]:
        run = self.get(run_id)
        with self._connect() as connection:
            connection.execute(
                "UPDATE deployments SET status = 'running' WHERE id = ?",
                (run_id,),
            )

        readbacks: list[dict[str, str]] = []
        for index, asset in enumerate(run["payload"]["assets"]):
            external_key = f"{run_id}:{asset['asset_id']}"
            self.provider.create_draft(
                external_key=external_key,
                asset=asset,
            )
            if crash_at == "after_first_provider_write" and index == 0:
                raise InjectedCrash(
                    "crashed after provider write and before local receipt"
                )
            readbacks.append(self.provider.read(external_key))

        receipt = {
            "run_id": run_id,
            "payload_sha256": run["payload_hash"],
            "objects": readbacks,
            "verified": True,
        }
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE deployments
                SET status = 'done', receipt_json = ?
                WHERE id = ?
                """,
                (_canonical_json(receipt), run_id),
            )
        return receipt

    def deployment_summary(self, run_id: str) -> dict[str, Any]:
        """Operator-facing summary of what a deployment did.

        This is what the dashboard and `make demo` show, and it is the
        quickest way to see the outcome of a run.
        """
        run = self.get(run_id)
        receipt = run.get("receipt") or {}
        objects = receipt.get("objects") or []
        return {
            "run_id": run_id,
            "status": run["status"],
            "objects_deployed": len(objects),
            "verified": bool(receipt.get("verified")),
            "assets_approved": len(run["payload"].get("assets", [])),
        }

    def audit(self, run_id: str) -> dict[str, Any]:
        """The dashboard's "Check again" button.

        Operators press this when they want reassurance that a deployment is
        still in the state the receipt describes.
        """
        run = self.get(run_id)
        receipt = run.get("receipt") or {}
        objects = receipt.get("objects") or []
        return {
            "run_id": run_id,
            "checked_objects": len(objects),
            "all_present": all(obj.get("object_id") for obj in objects),
            "verified": bool(receipt.get("verified")),
        }

    def recover(self) -> None:
        """
        Starter behavior: enough recovery for the happy-path demo.

        Operator evidence reports other cases that this does not make safe.
        """
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id FROM deployments WHERE status = 'running'"
            ).fetchall()
        for row in rows:
            self.run_once(str(row["id"]))
