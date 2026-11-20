"""Durable claims, lease fencing, dirty windows and atomic metric publication."""

from datetime import timedelta
from uuid import NAMESPACE_URL, uuid4, uuid5

from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects.sqlite import insert

from app.modules.alerts.service import publish_alerts, rule_snapshot, seed_defaults
from app.modules.alerts.service import SYSTEM_ACTOR
from app.contracts import DomainError
from app.modules.models.schema import normalize_input
from app.modules.monitoring.schema import (alerts, metrics, metric_revisions, processed, rules, windows, worker_state)
from app.modules.monitoring.statistics import CALCULATION_VERSION
from app.modules.monitoring.time_utils import parse_time, stamp
from app.modules.scoring.service import json_digest
from app.storage.sqlite_store import audit_logs, metadata, outbox, predictions, snapshots, storage_errors, versions

CONSUMER = "monitoring-v1"
MAX_ATTEMPTS = 5


class LeaseLost(Exception):
    pass


class InvalidEvent(Exception):
    pass


class MonitoringStore:
    def __init__(self, base, *, after_write=None):
        self.base, self.engine = base, base.engine
        self.after_write = after_write or (lambda stage: None)

    @storage_errors
    def initialize(self):
        metadata.create_all(self.engine)
        with self.base._write() as conn:
            columns = {r[1] for r in conn.exec_driver_sql("PRAGMA table_info(outbox_events)")}
            if "lease_token" not in columns:
                conn.exec_driver_sql("ALTER TABLE outbox_events ADD COLUMN lease_token VARCHAR")
            for version_id in conn.execute(select(versions.c.model_version_id)).scalars():
                seed_defaults(conn, version_id)

    @storage_errors
    def claim(self, now):
        at = stamp(now)
        expired = and_(outbox.c.status == "processing", or_(outbox.c.lease_expires_at.is_(None), outbox.c.lease_expires_at <= at))
        with self.base._write() as conn:
            conn.execute(outbox.update().where(outbox.c.event_type == "prediction.completed", expired, outbox.c.attempts >= MAX_ATTEMPTS).values(
                status="dead_letter", last_error="LEASE_EXHAUSTED", lease_token=None, lease_expires_at=None))
            eligible = or_(and_(outbox.c.status == "pending", or_(outbox.c.next_attempt_at.is_(None), outbox.c.next_attempt_at <= at)), expired)
            row = conn.execute(select(outbox).where(outbox.c.event_type == "prediction.completed", eligible,
                               outbox.c.attempts < MAX_ATTEMPTS).order_by(outbox.c.created_at, outbox.c.event_id).limit(1)).mappings().first()
            if row is None:
                return None
            token = str(uuid4())
            update = {"status": "processing", "attempts": row["attempts"] + 1, "lease_token": token,
                      "lease_expires_at": stamp(parse_time(now) + timedelta(seconds=30)), "next_attempt_at": None}
            conn.execute(outbox.update().where(outbox.c.event_id == row["event_id"]).values(**update))
            return {**dict(row), **update}

    @staticmethod
    def _fence(conn, claimed, now):
        row = conn.execute(select(outbox).where(outbox.c.event_id == claimed["event_id"])).mappings().one()
        if row["status"] != "processing" or row["lease_token"] != claimed["lease_token"] or row["lease_expires_at"] <= stamp(now):
            raise LeaseLost()
        return row

    @storage_errors
    def consume(self, claimed, now):
        at = stamp(now)
        with self.base._write() as conn:
            event = self._fence(conn, claimed, now)
            marker = conn.execute(select(processed).where(processed.c.consumer == CONSUMER, processed.c.event_id == event["event_id"])).first()
            if marker:
                conn.execute(outbox.update().where(outbox.c.event_id == event["event_id"]).values(status="done", lease_token=None,
                             lease_expires_at=None, last_error=None))
                return False
            record = conn.execute(select(predictions, snapshots.c.original_input, snapshots.c.normalized_input,
                                  snapshots.c.missing_fields, snapshots.c.content_sha256).join(snapshots)
                                  .where(predictions.c.prediction_id == event["prediction_id"])).mappings().one_or_none()
            if record is None or event["schema_version"] != "1.1":
                raise InvalidEvent()
            payload = event["payload"]
            expected = {"schema_version": "1.1", "event_id": event["event_id"], "event_type": "prediction.completed"}
            expected.update({k: record[k] for k in ("prediction_id", "snapshot_id", "model_version_id", "request_received_at",
                            "completed_at", "status", "risk_probability", "inference_ms", "inference_attempted", "error_code")})
            expected.update({k: record["response_json"][k] for k in ("decision_threshold", "decision")})
            if not isinstance(payload, dict) or any(k not in payload or payload[k] != v for k, v in expected.items()) or not isinstance(payload["inference_attempted"], bool):
                raise InvalidEvent()
            normalized = normalize_input(record["original_input"])
            if normalized != record["normalized_input"] or json_digest(record["original_input"]) != record["content_sha256"]:
                raise InvalidEvent()
            start = parse_time(record["request_received_at"]).replace(second=0, microsecond=0)
            end = start + timedelta(seconds=60)
            window_id = str(uuid5(NAMESPACE_URL, f"financial-ai:window:{record['model_version_id']}:{stamp(start)}"))
            current = conn.execute(select(windows).where(windows.c.window_id == window_id)).mappings().first()
            if not current:
                conn.execute(windows.insert().values(window_id=window_id, model_version_id=record["model_version_id"],
                             window_start=stamp(start), window_end=stamp(end), revision=0, computed_revision=0,
                             event_count=0, success_count=0, failure_count=0, attempted_count=0,
                             rule_snapshot=rule_snapshot(conn, record["model_version_id"], stamp(start)), status="collecting",
                             calculation_attempts=0))
            conn.execute(processed.insert().values(consumer=CONSUMER, event_id=event["event_id"], window_id=window_id, processed_at=at))
            self.after_write("processed")
            conn.execute(windows.update().where(windows.c.window_id == window_id).values(revision=windows.c.revision + 1,
                         event_count=windows.c.event_count + 1, success_count=windows.c.success_count + int(record["status"] == "success"),
                         failure_count=windows.c.failure_count + int(record["status"] == "failed"),
                         attempted_count=windows.c.attempted_count + int(record["inference_attempted"]),
                         status="dirty", calculation_attempts=0, next_calculation_at=None, last_error=None))
            self.after_write("window")
            conn.execute(outbox.update().where(outbox.c.event_id == event["event_id"]).values(status="done", lease_token=None,
                         lease_expires_at=None, next_attempt_at=None, last_error=None))
            self.after_write("event_done")
            return True

    @storage_errors
    def fail_event(self, claimed, now, code):
        with self.base._write() as conn:
            row = conn.execute(select(outbox).where(outbox.c.event_id == claimed["event_id"])).mappings().one()
            if (row["status"] != "processing" or row["lease_token"] != claimed["lease_token"]
                    or not row["lease_expires_at"] or row["lease_expires_at"] <= stamp(now)):
                return
            dead = row["attempts"] >= MAX_ATTEMPTS
            conn.execute(outbox.update().where(outbox.c.event_id == row["event_id"]).values(status="dead_letter" if dead else "pending",
                         lease_token=None, lease_expires_at=None, last_error=code,
                         next_attempt_at=None if dead else stamp(parse_time(now) + timedelta(seconds=min(60, 2 ** (row["attempts"] - 1))))))

    @storage_errors
    def ready_windows(self, now, limit=20):
        with self.engine.connect() as conn:
            return [dict(r) for r in conn.execute(select(windows).where(windows.c.window_end <= stamp(parse_time(now) - timedelta(seconds=10)),
                    windows.c.revision > windows.c.computed_revision, windows.c.calculation_attempts < MAX_ATTEMPTS,
                    or_(windows.c.next_calculation_at.is_(None), windows.c.next_calculation_at <= stamp(now)))
                    .order_by(windows.c.window_start).limit(limit)).mappings()]

    @storage_errors
    def window_data(self, window_id):
        with self.engine.connect() as conn:
            window = dict(conn.execute(select(windows).where(windows.c.window_id == window_id)).mappings().one())
            version = dict(conn.execute(select(versions).where(versions.c.model_version_id == window["model_version_id"])).mappings().one())
            records = [dict(r) for r in conn.execute(select(predictions.c.status, predictions.c.risk_probability,
                       predictions.c.inference_ms, predictions.c.inference_attempted, snapshots.c.normalized_input)
                       .select_from(processed.join(outbox, processed.c.event_id == outbox.c.event_id)
                                    .join(predictions, outbox.c.prediction_id == predictions.c.prediction_id)
                                    .join(snapshots, predictions.c.snapshot_id == snapshots.c.snapshot_id))
                       .where(processed.c.consumer == CONSUMER, processed.c.window_id == window_id)).mappings()]
            return window, version, records

    @storage_errors
    def publish(self, window, rows, reference_digest, now):
        at = stamp(now)
        with self.base._write() as conn:
            live = conn.execute(select(windows).where(windows.c.window_id == window["window_id"])).mappings().one()
            if live["revision"] != window["revision"] or live["computed_revision"] >= window["revision"]:
                return False
            for row in rows:
                values = {**row, "metric_id": str(uuid5(NAMESPACE_URL, f"{window['window_id']}:{row['metric_name']}:{row['feature']}")),
                          "window_id": window["window_id"], "model_version_id": window["model_version_id"],
                          "window_start": window["window_start"], "window_end": window["window_end"],
                          "window_revision": window["revision"], "reference_manifest_sha256": reference_digest,
                          "calculation_version": CALCULATION_VERSION, "computed_at": at}
                command = insert(metrics).values(**values)
                conn.execute(command.on_conflict_do_update(index_elements=["window_id", "metric_name", "feature"], set_=values))
                conn.execute(metric_revisions.insert().values(window_id=window["window_id"], window_revision=window["revision"],
                             metric_name=row["metric_name"], feature=row["feature"], record=values))
            self.after_write("metrics")
            publish_alerts(conn, window, rows, at)
            self.after_write("alerts")
            conn.execute(windows.update().where(windows.c.window_id == window["window_id"]).values(
                         computed_revision=window["revision"], status="computed", computed_at=at,
                         calculation_attempts=0, next_calculation_at=None, last_error=None))
            self.after_write("window_computed")
            return True

    @storage_errors
    def fail_window(self, window, now, code):
        with self.base._write() as conn:
            live = conn.execute(select(windows).where(windows.c.window_id == window["window_id"])).mappings().one()
            if live["revision"] != window["revision"] or live["computed_revision"] >= window["revision"]:
                return
            attempts = live["calculation_attempts"] + 1
            conn.execute(windows.update().where(windows.c.window_id == window["window_id"]).values(
                         calculation_attempts=attempts, status="calculation_failed" if attempts >= MAX_ATTEMPTS else "retrying",
                         last_error=code, next_calculation_at=stamp(parse_time(now) + timedelta(seconds=min(60, 2 ** (attempts - 1))))))

    @storage_errors
    def heartbeat(self, worker_id, now, result):
        with self.base._write() as conn:
            values = {"consumer": CONSUMER, "worker_id": worker_id, "last_seen_at": stamp(now), "last_result": result}
            conn.execute(insert(worker_state).values(**values).on_conflict_do_update(index_elements=["consumer"], set_=values))

    @storage_errors
    def retry_failed(self, kind, resource_id, reason, now):
        """Explicit local-operator action; never automatically revive quarantined work."""
        if kind not in {"event", "window"} or not reason.strip() or len(reason) > 1000:
            raise ValueError("a supported resource and a nonempty reason up to 1000 characters are required")
        table, identifier = (outbox, outbox.c.event_id) if kind == "event" else (windows, windows.c.window_id)
        required_status = "dead_letter" if kind == "event" else "calculation_failed"
        with self.base._write() as conn:
            row = conn.execute(select(table).where(identifier == resource_id)).mappings().first()
            if row is None:
                raise DomainError(404, "RESOURCE_NOT_FOUND", "The failed resource does not exist.")
            if row["status"] != required_status:
                raise DomainError(409, "RETRY_STATE_CONFLICT", "Only quarantined work can be manually retried.")
            update = {"status": "pending", "attempts": 0, "next_attempt_at": None, "lease_token": None,
                      "lease_expires_at": None, "last_error": None} if kind == "event" else {
                      "status": "dirty", "calculation_attempts": 0, "next_calculation_at": None, "last_error": None}
            conn.execute(table.update().where(identifier == resource_id).values(**update))
            details = {"reason": reason, "previous_status": row["status"], "previous_error": row["last_error"],
                       "previous_attempts": row["attempts" if kind == "event" else "calculation_attempts"]}
            conn.execute(audit_logs.insert().values(audit_id=str(uuid4()), principal_id=SYSTEM_ACTOR,
                         action=f"monitor_operator.{kind}_retry", resource_id=resource_id, created_at=stamp(now), details=details))
            return {"kind": kind, "resource_id": resource_id, "status": update["status"], "reason": reason}

    @storage_errors
    def status(self, now):
        with self.engine.connect() as conn:
            counts = {s: 0 for s in ("pending", "processing", "done", "dead_letter")}
            counts.update({s: count for s, count in conn.execute(select(outbox.c.status, func.count()).group_by(outbox.c.status))})
            heartbeat = conn.execute(select(worker_state).where(worker_state.c.consumer == CONSUMER)).mappings().first()
            age = (parse_time(now) - parse_time(heartbeat["last_seen_at"])).total_seconds() if heartbeat else None
            failures = [dict(r) for r in conn.execute(select(outbox.c.event_id, outbox.c.attempts, outbox.c.last_error)
                         .where(outbox.c.status == "dead_letter").limit(20)).mappings()]
            dirty = conn.execute(select(func.count()).select_from(windows).where(windows.c.revision > windows.c.computed_revision)).scalar_one()
            failed_windows = conn.execute(select(func.count()).select_from(windows).where(windows.c.status == "calculation_failed")).scalar_one()
            calculation_errors = [dict(r) for r in conn.execute(select(windows.c.window_id, windows.c.model_version_id,
                                  windows.c.status, windows.c.calculation_attempts, windows.c.last_error)
                                  .where(windows.c.last_error.is_not(None)).order_by(windows.c.window_start).limit(20)).mappings()]
            return {"queue": counts, "dirty_windows": dirty, "failed_windows": failed_windows, "dead_letters": failures,
                    "window_errors": calculation_errors,
                    "worker": {"state": "never_started" if not heartbeat else "fresh" if 0 <= age <= 15 else "stale",
                               "last_seen_at": heartbeat["last_seen_at"] if heartbeat else None,
                               "last_result": heartbeat["last_result"] if heartbeat else None}}
