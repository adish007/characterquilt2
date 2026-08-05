from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator


class InjectedCrash(RuntimeError):
    pass


class IdempotencyConflict(RuntimeError):
    pass


class RequestValidationError(ValueError):
    pass


class RunCancelled(RuntimeError):
    pass


class RunClaimed(RuntimeError):
    pass


class ClaimLost(RuntimeError):
    pass


class _ClosingConnection(sqlite3.Connection):
    """Make ``with self._connect()`` close as well as transact."""

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> bool:
        try:
            return bool(super().__exit__(exc_type, exc_value, traceback))
        finally:
            self.close()


class _ProviderCoordinator:
    """Serialize the provider's whole-file operations across relay processes."""

    def __init__(self, state_path: Path) -> None:
        state_path = Path(state_path)
        self.lock_path = state_path.with_name(f"{state_path.name}.lock")

    @contextmanager
    def hold(self) -> Iterator[None]:
        descriptor = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


@dataclass(frozen=True, slots=True)
class SubmissionResult:
    run_id: str
    replayed: bool
    matching_payload_run_ids: tuple[str, ...]


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
    def __init__(
        self,
        db_path: Path,
        provider_state_path: Path,
        *,
        clock: Callable[[], float] | None = None,
        claim_ttl_seconds: float = 30.0,
    ) -> None:
        if claim_ttl_seconds <= 0:
            raise ValueError("claim_ttl_seconds must be positive")
        self.db_path = Path(db_path)
        provider_state_path = Path(provider_state_path).resolve()
        self._provider_coordinator = _ProviderCoordinator(provider_state_path)
        if provider_state_path.exists():
            self.provider = FakeHubSpot(provider_state_path)
        else:
            with self._provider_coordinator.hold():
                self.provider = FakeHubSpot(provider_state_path)
        self._clock = clock or time.time
        self.claim_ttl_seconds = float(claim_ttl_seconds)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.db_path,
            factory=_ClosingConnection,
        )
        connection.row_factory = sqlite3.Row
        return connection

    def _init_db(self) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
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
            columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(deployments)"
                ).fetchall()
            }
            for name, column_type in (
                ("error_json", "TEXT"),
                ("claim_token", "TEXT"),
                ("claim_expires_at", "REAL"),
            ):
                if name not in columns:
                    connection.execute(
                        f"ALTER TABLE deployments ADD COLUMN {name} {column_type}"
                    )
            duplicate_keys = connection.execute(
                """
                SELECT idempotency_key, COUNT(*) AS uses
                FROM deployments
                GROUP BY idempotency_key
                HAVING COUNT(*) > 1
                ORDER BY idempotency_key
                """
            ).fetchall()
            if duplicate_keys:
                details = ", ".join(
                    f"{row['idempotency_key']!r} ({row['uses']} rows)"
                    for row in duplicate_keys
                )
                raise RuntimeError(
                    "cannot enforce idempotency-key uniqueness; resolve "
                    f"historical duplicate deployment keys first: {details}"
                )
            try:
                connection.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS
                        deployments_idempotency_key_unique
                    ON deployments (idempotency_key)
                    """
                )
            except sqlite3.IntegrityError as error:
                raise RuntimeError(
                    "cannot enforce idempotency-key uniqueness because "
                    "historical duplicate deployment keys exist"
                ) from error

    def _validate_idempotency_key(self, idempotency_key: str) -> None:
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise RequestValidationError(
                "idempotency_key must be a non-empty string"
            )

    def _validate_request(
        self,
        idempotency_key: str,
        payload: dict[str, Any],
    ) -> None:
        self._validate_idempotency_key(idempotency_key)
        if not isinstance(payload, dict):
            raise RequestValidationError("payload must be an object")
        if payload.get("destination") != "hubspot-marketing":
            raise RequestValidationError(
                "destination must be 'hubspot-marketing'"
            )
        if payload.get("mode") != "draft":
            raise RequestValidationError("mode must be 'draft'")

        assets = payload.get("assets")
        if not isinstance(assets, list):
            raise RequestValidationError("assets must be an explicit list")

        seen_asset_ids: set[str] = set()
        required_fields = (
            "asset_id",
            "source_sha256",
            "type",
            "display_name",
        )
        for index, asset in enumerate(assets):
            if not isinstance(asset, dict):
                raise RequestValidationError(f"asset {index} must be an object")
            for field in required_fields:
                value = asset.get(field)
                if not isinstance(value, str) or not value.strip():
                    raise RequestValidationError(
                        f"asset {index} field {field!r} must be a non-empty string"
                    )

            asset_id = asset["asset_id"]
            if asset_id in seen_asset_ids:
                raise RequestValidationError(
                    f"asset IDs must be unique; duplicate {asset_id!r}"
                )
            seen_asset_ids.add(asset_id)

            display_name = asset["display_name"]
            stored_name = display_name.strip()[: FakeHubSpot.DISPLAY_NAME_LIMIT]
            if display_name != stored_name:
                raise RequestValidationError(
                    f"asset {asset_id!r} display_name must already satisfy "
                    f"the provider's {FakeHubSpot.DISPLAY_NAME_LIMIT}-character "
                    "limit and whitespace rules"
                )

    def submit(
        self,
        idempotency_key: str,
        payload: dict[str, Any],
    ) -> SubmissionResult:
        self._validate_idempotency_key(idempotency_key)
        try:
            payload_json = _canonical_json(payload)
        except (TypeError, ValueError) as error:
            raise RequestValidationError(
                "payload must be JSON-serializable"
            ) from error
        payload_hash = _digest_text(payload_json)
        run_id = str(uuid.uuid4())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT id, payload_hash
                FROM deployments
                WHERE idempotency_key = ?
                """,
                (idempotency_key,),
            ).fetchone()
            matching_rows = connection.execute(
                """
                SELECT id
                FROM deployments
                WHERE payload_hash = ? AND idempotency_key <> ?
                ORDER BY rowid
                """,
                (payload_hash, idempotency_key),
            ).fetchall()
            matching_run_ids = tuple(str(row["id"]) for row in matching_rows)
            if existing is not None:
                if existing["payload_hash"] != payload_hash:
                    raise IdempotencyConflict(
                        f"idempotency key {idempotency_key!r} is already bound "
                        "to a different payload"
                    )
                return SubmissionResult(
                    run_id=str(existing["id"]),
                    replayed=True,
                    matching_payload_run_ids=matching_run_ids,
                )
            # Existing immutable bindings are resolved above. Validation still
            # occurs before a new key is reserved, so a corrected request can
            # reuse a key whose earlier submission was rejected.
            self._validate_request(idempotency_key, payload)
            connection.execute(
                """
                INSERT INTO deployments
                    (id, idempotency_key, payload_hash, payload_json, status)
                VALUES (?, ?, ?, ?, 'pending')
                """,
                (run_id, idempotency_key, payload_hash, payload_json),
            )
        return SubmissionResult(
            run_id=run_id,
            replayed=False,
            matching_payload_run_ids=matching_run_ids,
        )

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
        error_json = result.pop("error_json")
        result["error"] = (
            json.loads(error_json) if error_json is not None else None
        )
        result.pop("claim_token")
        return result

    def cancel(self, run_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE deployments SET status = 'cancelled' WHERE id = ?",
                (run_id,),
            )

    def retry(self, run_id: str) -> str:
        """Return the same logical run, requeuing only unfinished work."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT status, claim_token, claim_expires_at
                FROM deployments
                WHERE id = ?
                """,
                (run_id,),
            ).fetchone()
            if row is None:
                raise KeyError(run_id)

            status = str(row["status"])
            if status == "done":
                return run_id
            if status == "cancelled":
                raise RunCancelled(run_id)
            if status == "failed":
                raise RuntimeError(f"run {run_id!r} is terminally failed")

            now = float(self._clock())
            claim_is_active = (
                status == "running"
                and row["claim_token"] is not None
                and row["claim_expires_at"] is not None
                and float(row["claim_expires_at"]) > now
            )
            if claim_is_active:
                raise RunClaimed(f"run {run_id!r} has an active claim")
            if status not in {"pending", "retryable", "running"}:
                raise RuntimeError(
                    f"run {run_id!r} in state {status!r} cannot be retried"
                )

            cursor = connection.execute(
                """
                UPDATE deployments
                SET status = 'pending', claim_token = NULL,
                    claim_expires_at = NULL
                WHERE id = ?
                """,
                (run_id,),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"failed to requeue run {run_id!r}")
        return run_id

    def _renew_claim(self, run_id: str, claim_token: str) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            now = float(self._clock())
            cursor = connection.execute(
                """
                UPDATE deployments
                SET claim_expires_at = ?
                WHERE id = ? AND status = 'running'
                    AND claim_token = ? AND claim_expires_at > ?
                """,
                (
                    now + self.claim_ttl_seconds,
                    run_id,
                    claim_token,
                    now,
                ),
            )
            if cursor.rowcount != 1:
                raise ClaimLost(f"claim for run {run_id!r} is no longer active")

    def _record_error(
        self,
        run_id: str,
        claim_token: str,
        error: Exception,
    ) -> None:
        is_terminal = isinstance(
            error,
            (IdempotencyConflict, RequestValidationError),
        )
        status = "failed" if is_terminal else "retryable"
        error_json = _canonical_json(
            {
                "type": type(error).__name__,
                "message": str(error),
                "retryable": not is_terminal,
            }
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            now = float(self._clock())
            cursor = connection.execute(
                """
                UPDATE deployments
                SET status = ?, error_json = ?, receipt_json = NULL,
                    claim_token = NULL, claim_expires_at = NULL
                WHERE id = ? AND status = 'running'
                    AND claim_token = ? AND claim_expires_at > ?
                """,
                (status, error_json, run_id, claim_token, now),
            )
            if cursor.rowcount != 1:
                raise ClaimLost(f"claim for run {run_id!r} is no longer active")

    def _complete(
        self,
        run_id: str,
        claim_token: str,
        receipt: dict[str, Any],
    ) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            now = float(self._clock())
            cursor = connection.execute(
                """
                UPDATE deployments
                SET status = 'done', receipt_json = ?, error_json = NULL,
                    claim_token = NULL, claim_expires_at = NULL
                WHERE id = ? AND status = 'running'
                    AND claim_token = ? AND claim_expires_at > ?
                """,
                (_canonical_json(receipt), run_id, claim_token, now),
            )
            if cursor.rowcount != 1:
                raise ClaimLost(f"claim for run {run_id!r} is no longer active")

    def run_once(
        self,
        run_id: str,
        *,
        crash_at: str | None = None,
    ) -> dict[str, Any]:
        claim_token = str(uuid.uuid4())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            now = float(self._clock())
            row = connection.execute(
                """
                SELECT status, receipt_json, claim_token, claim_expires_at
                FROM deployments
                WHERE id = ?
                """,
                (run_id,),
            ).fetchone()
            if row is None:
                raise KeyError(run_id)

            status = str(row["status"])
            if status == "done":
                if row["receipt_json"] is None:
                    raise RuntimeError(f"done run {run_id!r} has no receipt")
                return json.loads(str(row["receipt_json"]))
            if status == "cancelled":
                raise RunCancelled(run_id)
            if status == "failed":
                raise RuntimeError(f"run {run_id!r} is terminally failed")

            claim_is_active = (
                status == "running"
                and row["claim_token"] is not None
                and row["claim_expires_at"] is not None
                and float(row["claim_expires_at"]) > now
            )
            if claim_is_active:
                raise RunClaimed(f"run {run_id!r} has an active claim")
            if status not in {"pending", "retryable", "running"}:
                raise RuntimeError(
                    f"run {run_id!r} in state {status!r} cannot be executed"
                )

            cursor = connection.execute(
                """
                UPDATE deployments
                SET status = 'running', claim_token = ?, claim_expires_at = ?
                WHERE id = ?
                    AND (
                        status IN ('pending', 'retryable')
                        OR (
                            status = 'running'
                            AND (
                                claim_token IS NULL
                                OR claim_expires_at IS NULL
                                OR claim_expires_at <= ?
                            )
                        )
                    )
                """,
                (
                    claim_token,
                    now + self.claim_ttl_seconds,
                    run_id,
                    now,
                ),
            )
            if cursor.rowcount != 1:
                raise RunClaimed(f"run {run_id!r} could not be claimed")

        try:
            run = self.get(run_id)
            # Recheck stored requests at the execution boundary. This prevents
            # unfinished rows created before preflight validation was
            # introduced (or rows altered out of band) from bypassing it.
            self._validate_request(
                str(run["idempotency_key"]),
                run["payload"],
            )
            readbacks: list[dict[str, str]] = []
            for index, asset in enumerate(run["payload"]["assets"]):
                external_key = f"{run_id}:{asset['asset_id']}"
                with self._provider_coordinator.hold():
                    self._renew_claim(run_id, claim_token)
                    self.provider.create_draft(
                        external_key=external_key,
                        asset=asset,
                    )
                    if crash_at == "after_first_provider_write" and index == 0:
                        raise InjectedCrash(
                            "crashed after provider write and before local receipt"
                        )
                    self._renew_claim(run_id, claim_token)
                    readbacks.append(self.provider.read(external_key))

            receipt = {
                "run_id": run_id,
                "payload_sha256": run["payload_hash"],
                "objects": readbacks,
                "verified": True,
            }
            self._complete(run_id, claim_token, receipt)
            return receipt
        except InjectedCrash:
            raise
        except ClaimLost:
            raise
        except Exception as error:
            try:
                self._record_error(run_id, claim_token, error)
            except ClaimLost as claim_error:
                raise claim_error from error
            raise

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
            "error": run.get("error"),
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
        """Attempt each currently eligible run without blocking later work."""
        now = float(self._clock())
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id
                FROM deployments
                WHERE status IN ('pending', 'retryable')
                    OR (
                        status = 'running'
                        AND (
                            claim_token IS NULL
                            OR claim_expires_at IS NULL
                            OR claim_expires_at <= ?
                        )
                    )
                ORDER BY rowid
                """,
                (now,),
            ).fetchall()
        for row in rows:
            try:
                self.run_once(str(row["id"]))
            except Exception:
                continue
