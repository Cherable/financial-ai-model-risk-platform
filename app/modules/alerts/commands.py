"""Transactional rule versions and alert actions with command idempotency."""

from datetime import timedelta
import math
from uuid import uuid4

from sqlalchemy import func, select

from app.contracts import DomainError, StoredResponse
from app.modules.models.schema import FEATURES
from app.modules.monitoring.schema import alerts, alert_actions, alert_revisions, receipts, rules
from app.modules.monitoring.time_utils import parse_time, stamp
from app.modules.scoring.service import json_digest
from app.storage.sqlite_store import audit_logs, storage_errors


def validate_rule(name, feature, threshold, minimum):
    if name not in {"missing_rate", "feature_psi", "prediction_psi", "failure_rate", "inference_p95"}:
        raise DomainError(422, "INVALID_RULE", "Unknown metric.")
    if (name in {"missing_rate", "feature_psi"} and feature not in FEATURES) or (name not in {"missing_rate", "feature_psi"} and feature):
        raise DomainError(422, "INVALID_RULE", "The feature does not match the metric.")
    if not math.isfinite(threshold) or threshold < 0 or minimum < 1 or minimum > 100000:
        raise DomainError(422, "INVALID_RULE", "Invalid threshold or sample count.")
    if name in {"missing_rate", "failure_rate"} and threshold > 1:
        raise DomainError(422, "INVALID_RULE", "A ratio threshold must be in [0,1].")
    if name in {"feature_psi", "prediction_psi"} and minimum < 200:
        raise DomainError(422, "INVALID_RULE", "PSI requires at least 200 samples.")


class AlertCommands:
    def __init__(self, monitor):
        self.monitor, self.base = monitor, monitor.base

    @staticmethod
    def _previous(conn, actor, route, key, digest):
        row = conn.execute(select(receipts).where(receipts.c.principal_id == actor, receipts.c.route == route,
                                                 receipts.c.idempotency_key == key)).mappings().first()
        if row:
            if row["request_sha256"] != digest:
                raise DomainError(409, "IDEMPOTENCY_CONFLICT", "The key was used for different content.")
            return StoredResponse(row["response_json"], 200, True)
        return None

    @staticmethod
    def _receipt(conn, actor, route, key, digest, result):
        conn.execute(receipts.insert().values(principal_id=actor, route=route, idempotency_key=key,
                                             request_sha256=digest, response_json=result))

    @storage_errors
    def create_rule(self, actor, body, key, now):
        body = {**body, "feature": body.get("feature") or ""}
        try:
            effective = parse_time(body["effective_at"])
        except (ValueError, TypeError) as exc:
            raise DomainError(422, "INVALID_RULE", "An explicit effective timezone is required.") from exc
        body["effective_at"] = stamp(effective)
        validate_rule(body["metric_name"], body["feature"], body["threshold"], body["min_samples"])
        digest, route = json_digest(body), "/api/v1/monitor-rules"
        with self.base._write() as conn:
            previous = self._previous(conn, actor.principal_id, route, key, digest)
            if previous:
                return previous
            earliest = parse_time(now).replace(second=0, microsecond=0) + timedelta(seconds=60)
            if effective < earliest or effective.second or effective.microsecond:
                raise DomainError(422, "INVALID_EFFECTIVE_TIME", "Rules start at a future UTC minute boundary.")
            version = conn.execute(select(func.max(rules.c.rule_version)).where(rules.c.model_version_id == body["model_version_id"],
                        rules.c.metric_name == body["metric_name"], rules.c.feature == body["feature"])).scalar_one()
            result = {**body, "rule_id": str(uuid4()), "rule_version": (version or 0) + 1,
                      "created_at": stamp(now), "created_by": actor.principal_id}
            conn.execute(rules.insert().values(**result))
            conn.execute(audit_logs.insert().values(audit_id=str(uuid4()), principal_id=actor.principal_id,
                         action="monitor_rule.created", resource_id=result["rule_id"], created_at=stamp(now), details=result))
            self._receipt(conn, actor.principal_id, route, key, digest, result)
            return StoredResponse(result, 200)

    @storage_errors
    def act(self, actor, alert_id, action, note, key, now):
        route = "/api/v1/alerts/action"
        digest = json_digest({"alert_id": alert_id, "action": action, "note": note})
        with self.base._write() as conn:
            previous = self._previous(conn, actor.principal_id, route, key, digest)
            if previous:
                return previous
            alert = conn.execute(select(alerts).where(alerts.c.alert_id == alert_id)).mappings().one()
            expected = "open" if action == "acknowledge" else "acknowledged"
            target = "acknowledged" if action == "acknowledge" else "closed"
            if alert["status"] != expected:
                raise DomainError(409, "ALERT_STATE_CONFLICT", "The alert is not in the required workflow state.")
            if action == "close" and not note.strip():
                raise DomainError(422, "CLOSE_NOTE_REQUIRED", "Closing an alert requires a disposition note.")
            if action == "close" and alert["assigned_to"] != actor.principal_id and "admin" not in actor.roles:
                raise DomainError(403, "FORBIDDEN", "Only the assignee or an administrator can close the alert.")
            changes = {"status": target, "updated_at": stamp(now)}
            changes["assigned_to" if action == "acknowledge" else "close_note"] = actor.principal_id if action == "acknowledge" else note
            conn.execute(alerts.update().where(alerts.c.alert_id == alert_id).values(**changes))
            conn.execute(alert_actions.insert().values(action_id=str(uuid4()), alert_id=alert_id,
                         principal_id=actor.principal_id, from_status=expected, to_status=target, note=note, created_at=stamp(now)))
            conn.execute(audit_logs.insert().values(audit_id=str(uuid4()), principal_id=actor.principal_id,
                         action="alert." + target, resource_id=alert_id, created_at=stamp(now), details={"from": expected, "to": target, "note": note}))
            result = {**dict(alert), **changes}
            self._receipt(conn, actor.principal_id, route, key, digest, result)
            return StoredResponse(result, 200)
