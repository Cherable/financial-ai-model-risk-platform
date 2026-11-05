"""A4 observable backend; pages are left to A5."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from app.contracts import DomainError
from app.modules.alerts.commands import AlertCommands
from app.modules.monitoring.schema import alerts, alert_actions, alert_revisions, metrics, rules, windows
from app.modules.monitoring.time_utils import now_utc
from app.storage.sqlite_store import outbox, predictions, storage_errors


class RuleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_version_id: UUID
    metric_name: Literal["missing_rate", "feature_psi", "prediction_psi", "failure_rate", "inference_p95"]
    feature: str | None = None
    threshold: float = Field(ge=0, allow_inf_nan=False)
    min_samples: int = Field(ge=1, le=100000)
    effective_at: datetime


class ActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: str = Field(default="", max_length=1000)


def attach_routes(app, principal, idempotency_key, base, monitor):
    def ready():
        if monitor is None:
            raise DomainError(503, "MONITORING_UNAVAILABLE", "A monitoring storage adapter is required.")

    def permitted(actor, version_id, write=False):
        ready()
        version = base.version(version_id)
        if version["model_id"] not in actor.model_ids or (write and not actor.roles.intersection({"admin", "risk_manager"})):
            raise DomainError(403, "FORBIDDEN", "The role or model permission is insufficient.")

    def respond(result):
        return JSONResponse(content=result.body, status_code=result.http_status,
                            headers={"Idempotency-Replayed": str(result.replayed).lower()})

    @app.get("/api/v1/metrics")
    @storage_errors
    def get_metrics(model_version_id: UUID, limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0),
                    actor=Depends(principal)):
        permitted(actor, str(model_version_id))
        with monitor.engine.connect() as conn:
            rows = list(conn.execute(select(metrics, windows.c.revision.label("latest_window_revision"),
                               windows.c.status.label("window_status")).join(windows, metrics.c.window_id == windows.c.window_id)
                               .where(metrics.c.model_version_id == str(model_version_id))
                               .order_by(metrics.c.window_start.desc(), metrics.c.metric_name, metrics.c.feature).limit(limit).offset(offset)).mappings())
            backlog = {}
            for row in rows:
                if row["window_id"] not in backlog:
                    backlog[row["window_id"]] = conn.execute(select(func.count()).select_from(outbox.join(predictions))
                        .where(outbox.c.status != "done", outbox.c.event_type == "prediction.completed",
                               predictions.c.model_version_id == str(model_version_id),
                               predictions.c.request_received_at >= row["window_start"],
                               predictions.c.request_received_at < row["window_end"])).scalar_one()
            return {"items": [{**dict(row), "unconsumed_events": backlog[row["window_id"]],
                               "stale": row["window_revision"] != row["latest_window_revision"] or backlog[row["window_id"]] > 0} for row in rows]}

    @app.get("/api/v1/monitor-rules")
    @storage_errors
    def get_rules(model_version_id: UUID, limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0), actor=Depends(principal)):
        permitted(actor, str(model_version_id))
        with monitor.engine.connect() as conn:
            return {"items": [dict(row) for row in conn.execute(select(rules).where(rules.c.model_version_id == str(model_version_id))
                              .order_by(rules.c.effective_at.desc(), rules.c.rule_version.desc()).limit(limit).offset(offset)).mappings()]}

    @app.post("/api/v1/monitor-rules")
    def create_rule(body: RuleRequest, actor=Depends(principal), key=Depends(idempotency_key)):
        permitted(actor, str(body.model_version_id), True)
        values = body.model_dump()
        values["model_version_id"] = str(body.model_version_id)
        return respond(AlertCommands(monitor).create_rule(actor, values, key, now_utc()))

    @app.get("/api/v1/alerts")
    @storage_errors
    def get_alerts(model_version_id: UUID, limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0), actor=Depends(principal)):
        permitted(actor, str(model_version_id))
        with monitor.engine.connect() as conn:
            return {"items": [dict(row) for row in conn.execute(select(alerts).where(alerts.c.model_version_id == str(model_version_id))
                              .order_by(alerts.c.created_at.desc(), alerts.c.alert_id).limit(limit).offset(offset)).mappings()]}

    @app.get("/api/v1/alerts/{alert_id}")
    @storage_errors
    def get_alert(alert_id: UUID, actor=Depends(principal)):
        ready()
        with monitor.engine.connect() as conn:
            alert = conn.execute(select(alerts).where(alerts.c.alert_id == str(alert_id))).mappings().first()
            if not alert:
                raise DomainError(404, "ALERT_NOT_FOUND", "The alert does not exist.")
            permitted(actor, alert["model_version_id"])
            return {**dict(alert), "revisions": [row[0] for row in conn.execute(select(alert_revisions.c.record)
                    .where(alert_revisions.c.alert_id == str(alert_id)).order_by(alert_revisions.c.window_revision.desc()).limit(100))],
                    "actions": [dict(row) for row in conn.execute(select(alert_actions).where(alert_actions.c.alert_id == str(alert_id))
                                .order_by(alert_actions.c.created_at).limit(100)).mappings()]}

    def act(actor, alert_id, action, note, key):
        ready()
        with monitor.engine.connect() as conn:
            alert = conn.execute(select(alerts).where(alerts.c.alert_id == alert_id)).mappings().first()
            if not alert:
                raise DomainError(404, "ALERT_NOT_FOUND", "The alert does not exist.")
            permitted(actor, alert["model_version_id"], True)
        return respond(AlertCommands(monitor).act(actor, alert_id, action, note, key, now_utc()))

    @app.post("/api/v1/alerts/{alert_id}/acknowledge")
    @storage_errors
    def acknowledge(alert_id: UUID, body: ActionRequest, actor=Depends(principal), key=Depends(idempotency_key)):
        return act(actor, str(alert_id), "acknowledge", body.note, key)

    @app.post("/api/v1/alerts/{alert_id}/close")
    @storage_errors
    def close(alert_id: UUID, body: ActionRequest, actor=Depends(principal), key=Depends(idempotency_key)):
        return act(actor, str(alert_id), "close", body.note, key)

    @app.get("/api/v1/monitor/status")
    def status(actor=Depends(principal)):
        ready()
        if "admin" not in actor.roles:
            raise DomainError(403, "FORBIDDEN", "Administrator access is required.")
        return monitor.status(now_utc())
