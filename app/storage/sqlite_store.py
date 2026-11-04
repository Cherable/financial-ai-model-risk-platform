"""SQLite/SQLAlchemy reference adapter implementing a single atomic score write.

No worker, users database, audit export or production migrations are implemented here.
"""

from contextlib import contextmanager
from decimal import Decimal
from functools import wraps
from pathlib import Path
from uuid import uuid4

from sqlalchemy import (JSON, Boolean, CheckConstraint, Column, Float, ForeignKey, Integer,
                        MetaData, String, Table, Text, UniqueConstraint, create_engine, event, func, select)
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

from app.contracts import DomainError, StorageUnavailable, StoredResponse

metadata = MetaData()
models = Table("models", metadata, Column("model_id", String, primary_key=True), Column("name", String, nullable=False))
versions = Table("model_versions", metadata,
                 Column("model_version_id", String, primary_key=True),
                 Column("model_id", ForeignKey("models.model_id"), nullable=False),
                 Column("version", String, nullable=False), Column("manifest_sha256", String, nullable=False),
                 Column("status", String, nullable=False), UniqueConstraint("model_id", "version"))
activations = Table("model_activations", metadata,
                    Column("model_id", ForeignKey("models.model_id"), primary_key=True),
                    Column("model_version_id", ForeignKey("model_versions.model_version_id"), nullable=False),
                    Column("activated_at", String, nullable=False))
snapshots = Table("input_snapshots", metadata, Column("snapshot_id", String, primary_key=True),
                  Column("original_input", JSON, nullable=False), Column("normalized_input", JSON, nullable=False),
                  Column("missing_fields", JSON, nullable=False), Column("content_sha256", String, nullable=False),
                  Column("monthly_income_cents", Integer), Column("loan_amount_cents", Integer))
predictions = Table("predictions", metadata,
                    Column("prediction_id", String, primary_key=True),
                    Column("principal_id", String, nullable=False), Column("route", String, nullable=False),
                    Column("idempotency_key", String, nullable=False), Column("request_sha256", String, nullable=False),
                    Column("model_version_id", ForeignKey("model_versions.model_version_id"), nullable=False),
                    Column("snapshot_id", ForeignKey("input_snapshots.snapshot_id"), nullable=False),
                    Column("status", String, nullable=False), Column("risk_probability", Float),
                    Column("error_code", String), Column("inference_ms", Float),
                    Column("inference_attempted", Boolean, nullable=False),
                    Column("request_received_at", String, nullable=False), Column("completed_at", String, nullable=False),
                    Column("explanation_snapshot", JSON), Column("http_status", Integer, nullable=False),
                    Column("response_json", JSON, nullable=False),
                    UniqueConstraint("principal_id", "route", "idempotency_key"),
                    CheckConstraint("(status = 'success' AND risk_probability IS NOT NULL AND risk_probability >= 0 AND risk_probability <= 1 AND error_code IS NULL) OR (status = 'failed' AND risk_probability IS NULL AND error_code IS NOT NULL)"),
                    CheckConstraint("(inference_attempted = 1 AND inference_ms IS NOT NULL AND inference_ms >= 0) OR (inference_attempted = 0 AND inference_ms IS NULL)"))
audit_logs = Table("audit_logs", metadata, Column("audit_id", String, primary_key=True),
                   Column("principal_id", String, nullable=False), Column("action", String, nullable=False),
                   Column("resource_id", String, nullable=False), Column("created_at", String, nullable=False),
                   Column("details", JSON, nullable=False))
outbox = Table("outbox_events", metadata, Column("event_id", String, primary_key=True),
               Column("prediction_id", ForeignKey("predictions.prediction_id"), nullable=False, unique=True),
               Column("event_type", String, nullable=False), Column("schema_version", String, nullable=False),
               Column("payload", JSON, nullable=False), Column("status", String, nullable=False),
               Column("attempts", Integer, nullable=False), Column("created_at", String, nullable=False),
               Column("next_attempt_at", String), Column("lease_expires_at", String), Column("last_error", Text))
activation_receipts = Table("activation_receipts", metadata,
                            Column("principal_id", String, primary_key=True), Column("route", String, primary_key=True),
                            Column("idempotency_key", String, primary_key=True), Column("request_sha256", String, nullable=False),
                            Column("response_json", JSON, nullable=False), Column("http_status", Integer, nullable=False))


def storage_errors(method):
    @wraps(method)
    def wrapped(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except SQLAlchemyError as exc:
            raise StorageUnavailable() from exc
    return wrapped


class SqlitePredictionStore:
    def __init__(self, database_url: str, *, after_write=None):
        url = make_url(database_url)
        if url.drivername != "sqlite" or not url.database or url.database == ":memory:":
            raise ValueError("A3 adapter requires a local file-backed sqlite database")
        Path(url.database).resolve().parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 5})
        self.after_write = after_write or (lambda stage: None)

        @event.listens_for(self.engine, "connect")
        def configure(connection, _record):
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

    @contextmanager
    def _write(self):
        with self.engine.connect() as conn:
            conn.exec_driver_sql("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    @storage_errors
    def initialize(self, descriptors: list[dict]) -> None:
        metadata.create_all(self.engine)
        with self._write() as conn:
            for descriptor in descriptors:
                if not conn.execute(select(models).where(models.c.model_id == descriptor["model_id"])).first():
                    conn.execute(models.insert().values(model_id=descriptor["model_id"], name="Synthetic credit risk"))
                existing = conn.execute(select(versions).where(versions.c.model_version_id == descriptor["model_version_id"])).mappings().first()
                if existing:
                    if any(existing[k] != descriptor[k] for k in ("model_id", "version", "manifest_sha256")):
                        raise ValueError("registered version metadata cannot be overwritten")
                else:
                    conn.execute(versions.insert().values(**descriptor, status="validated"))

    def close(self):
        self.engine.dispose()

    def healthy(self) -> bool:
        try:
            with self.engine.connect() as conn:
                return conn.execute(select(1)).scalar_one() == 1
        except SQLAlchemyError:
            return False

    def _version(self, conn, version_id):
        row = conn.execute(select(versions).where(versions.c.model_version_id == version_id)).mappings().first()
        if row is None:
            raise DomainError(404, "VERSION_NOT_FOUND", "The model version does not exist.")
        pointer = conn.execute(select(activations.c.model_version_id).where(activations.c.model_id == row["model_id"])).scalar_one_or_none()
        return {**dict(row), "active": pointer == version_id}

    @storage_errors
    def version(self, version_id: str) -> dict:
        with self.engine.connect() as conn:
            return self._version(conn, version_id)

    @storage_errors
    def versions(self, model_ids: frozenset[str]) -> list[dict]:
        with self.engine.connect() as conn:
            ids = conn.execute(select(versions.c.model_version_id).where(versions.c.model_id.in_(model_ids))).scalars().all()
            return [self._version(conn, version_id) for version_id in ids]

    @staticmethod
    def _previous(conn, actor, route, key, digest):
        table = predictions if route == "/api/v1/predictions" else activation_receipts
        row = conn.execute(select(table).where(table.c.principal_id == actor, table.c.route == route,
                                               table.c.idempotency_key == key)).mappings().first()
        if row is None:
            return None
        if row["request_sha256"] != digest:
            raise DomainError(409, "IDEMPOTENCY_CONFLICT", "This key was already used for different content.")
        return StoredResponse(row["response_json"], row["http_status"], True)

    @storage_errors
    def previous(self, actor, route, key, digest):
        with self.engine.connect() as conn:
            return self._previous(conn, actor, route, key, digest)

    @storage_errors
    def activate(self, command: dict) -> StoredResponse:
        with self._write() as conn:
            previous = self._previous(conn, command["principal_id"], command["route"], command["key"], command["digest"])
            if previous:
                return previous
            version = self._version(conn, command["version_id"])
            if version["status"] != "validated":
                raise DomainError(409, "VERSION_NOT_VALIDATED", "Only validated versions can be activated.")
            conn.execute(activations.delete().where(activations.c.model_id == version["model_id"]))
            conn.execute(activations.insert().values(model_id=version["model_id"], model_version_id=command["version_id"],
                                                     activated_at=command["at"]))
            result = {"model_version_id": command["version_id"], "model_id": version["model_id"],
                      "version": version["version"], "active": True, "activated_at": command["at"]}
            conn.execute(audit_logs.insert().values(audit_id=str(uuid4()), principal_id=command["principal_id"],
                         action="model_version.activated", resource_id=command["version_id"], created_at=command["at"],
                         details={"version": version["version"], "manifest_sha256": version["manifest_sha256"]}))
            self.after_write("activation_audit")
            conn.execute(activation_receipts.insert().values(principal_id=command["principal_id"], route=command["route"],
                         idempotency_key=command["key"], request_sha256=command["digest"], response_json=result, http_status=200))
            return StoredResponse(result, 200)

    @storage_errors
    def save_prediction(self, command: dict) -> StoredResponse:
        with self._write() as conn:
            previous = self._previous(conn, command["principal_id"], command["route"], command["key"], command["digest"])
            if previous:
                return previous
            snapshot_id, prediction_id, event_id = (str(uuid4()) for _ in range(3))
            outcome = command["outcome"]
            normalized = command["normalized_input"]
            cents = lambda key: None if normalized[key] is None else int(Decimal(normalized[key]) * 100)
            conn.execute(snapshots.insert().values(snapshot_id=snapshot_id, original_input=command["original_input"],
                         normalized_input=normalized, missing_fields=command["missing_fields"],
                         content_sha256=command["input_sha256"], monthly_income_cents=cents("monthly_income"),
                         loan_amount_cents=cents("loan_amount")))
            self.after_write("snapshot")
            body = {"prediction_id": prediction_id, "event_id": event_id, "snapshot_id": snapshot_id,
                    "model_version_id": command["version_id"], "model_version": command["version"],
                    "manifest_sha256": command["manifest_sha256"], "request_received_at": command["received_at"],
                    "completed_at": command["completed_at"], **outcome}
            status = 201 if outcome["status"] == "success" else 503
            conn.execute(predictions.insert().values(prediction_id=prediction_id, principal_id=command["principal_id"],
                         route=command["route"], idempotency_key=command["key"], request_sha256=command["digest"],
                         model_version_id=command["version_id"], snapshot_id=snapshot_id, status=outcome["status"],
                         risk_probability=outcome["risk_probability"], error_code=outcome["error_code"],
                         inference_ms=outcome["inference_ms"], inference_attempted=outcome["inference_attempted"],
                         request_received_at=command["received_at"], completed_at=command["completed_at"],
                         explanation_snapshot=outcome["explanation"], http_status=status, response_json=body))
            self.after_write("prediction")
            conn.execute(audit_logs.insert().values(audit_id=str(uuid4()), principal_id=command["principal_id"],
                         action="prediction.completed", resource_id=prediction_id, created_at=command["completed_at"],
                         details={"status": outcome["status"], "model_version_id": command["version_id"],
                                  "manifest_sha256": command["manifest_sha256"], "error_code": outcome["error_code"]}))
            self.after_write("audit")
            payload = {k: body[k] for k in ("prediction_id", "snapshot_id", "model_version_id", "request_received_at",
                       "completed_at", "status", "risk_probability", "decision_threshold", "decision", "inference_ms",
                       "inference_attempted", "error_code")}
            payload.update(schema_version="1.1", event_id=event_id, event_type="prediction.completed")
            conn.execute(outbox.insert().values(event_id=event_id, prediction_id=prediction_id,
                         event_type="prediction.completed", schema_version="1.1", payload=payload,
                         status="pending", attempts=0, created_at=command["completed_at"]))
            self.after_write("outbox")
            return StoredResponse(body, status)
