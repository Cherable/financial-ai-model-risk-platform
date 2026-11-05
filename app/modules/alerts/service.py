"""Default policies and correction-preserving alert publication."""

from uuid import NAMESPACE_URL, uuid4, uuid5

from sqlalchemy import select

from app.modules.models.schema import FEATURES
from app.modules.monitoring.schema import alerts, alert_revisions, rules
from app.storage.sqlite_store import audit_logs

SYSTEM_ACTOR = str(uuid5(NAMESPACE_URL, "financial-ai:monitoring-system"))


def seed_defaults(conn, version_id):
    defaults = [("missing_rate", f, 0.05, 1) for f in FEATURES]
    defaults += [("feature_psi", f, 0.2, 200) for f in FEATURES]
    defaults += [("prediction_psi", "", 0.2, 200), ("failure_rate", "", 0.01, 1), ("inference_p95", "", 500.0, 1)]
    for name, feature, threshold, minimum in defaults:
        existing = conn.execute(select(rules.c.rule_id).where(rules.c.model_version_id == version_id,
                   rules.c.metric_name == name, rules.c.feature == feature)).first()
        if not existing:
            rule_id = str(uuid5(NAMESPACE_URL, f"financial-ai:rule:{version_id}:{name}:{feature}:1"))
            conn.execute(rules.insert().values(rule_id=rule_id, model_version_id=version_id, metric_name=name,
                         feature=feature, rule_version=1, threshold=threshold, min_samples=minimum,
                         effective_at="1970-01-01T00:00:00.000000Z", created_at="1970-01-01T00:00:00.000000Z",
                         created_by=SYSTEM_ACTOR))


def rule_snapshot(conn, version_id, start):
    rows = conn.execute(select(rules).where(rules.c.model_version_id == version_id, rules.c.effective_at <= start)
                        .order_by(rules.c.effective_at.desc(), rules.c.rule_version.desc())).mappings()
    selected = {}
    for row in rows:
        selected.setdefault((row["metric_name"], row["feature"]), dict(row))
    return list(selected.values())


def publish_alerts(conn, window, rows, at):
    by_key = {(r["metric_name"], r["feature"]): r for r in rows}
    created = 0
    for rule in window["rule_snapshot"]:
        metric = by_key[(rule["metric_name"], rule["feature"])]
        triggered = (metric["status"] == "ready" and metric["sample_count"] >= rule["min_samples"]
                     and metric["value"] > rule["threshold"])
        existing = conn.execute(select(alerts).where(alerts.c.window_id == window["window_id"],
                                alerts.c.rule_id == rule["rule_id"])).mappings().first()
        if not triggered and not existing:
            continue
        alert_id = existing["alert_id"] if existing else str(uuid4())
        correction = {"value": metric["value"], "sample_count": metric["sample_count"],
                      "condition_active": bool(triggered), "window_revision": window["revision"], "updated_at": at}
        if existing:
            conn.execute(alerts.update().where(alerts.c.alert_id == alert_id).values(**correction))
        else:
            conn.execute(alerts.insert().values(alert_id=alert_id, window_id=window["window_id"],
                         model_version_id=window["model_version_id"], rule_id=rule["rule_id"],
                         metric_name=rule["metric_name"], feature=rule["feature"], rule_snapshot=rule,
                         status="open", created_at=at, **correction))
            created += 1
        revision = {**correction, "threshold": rule["threshold"], "rule_id": rule["rule_id"],
                    "reason": "late_recalculation" if existing else "initial_trigger"}
        conn.execute(alert_revisions.insert().values(alert_id=alert_id, window_revision=window["revision"], record=revision))
        conn.execute(audit_logs.insert().values(audit_id=str(uuid4()), principal_id=SYSTEM_ACTOR,
                     action="monitor_alert.corrected" if existing else "monitor_alert.created", resource_id=alert_id,
                     created_at=at, details=revision))
    return created
