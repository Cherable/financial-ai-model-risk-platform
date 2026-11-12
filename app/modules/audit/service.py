"""评分证据的读取、检索与导出。

只读：本模块不修改 predictions 与 input_snapshots。查询与导出动作本身写 audit_logs。
"""

import csv
import hashlib
import io
import json
from datetime import datetime, timezone
from uuid import NAMESPACE_URL, uuid4, uuid5

from sqlalchemy import and_, insert, select

from app.contracts import DomainError
from app.db.schema import audit_exports
from app.modules.monitoring.schema import alert_actions, alerts
from app.modules.monitoring.time_utils import parse_time, stamp, window_bounds
from app.storage.sqlite_store import audit_logs, predictions, snapshots, versions

FULL_VIEW_ROLES = {"auditor", "admin"}
MAX_RANGE_DAYS = 31


def require_scope(actor, model_id: str) -> None:
    if model_id not in actor.model_ids:
        raise DomainError(403, "FORBIDDEN", "Model is outside the authorized range.")


def model_id_of(conn, version_id: str) -> str | None:
    row = conn.execute(select(versions.c.model_id)
                       .where(versions.c.model_version_id == version_id)).first()
    return row[0] if row else None


def load_prediction(conn, prediction_id: str) -> dict:
    row = conn.execute(select(predictions)
                       .where(predictions.c.prediction_id == prediction_id)).mappings().first()
    if row is None:
        raise DomainError(404, "PREDICTION_NOT_FOUND", "prediction does not exist")
    return dict(row)


def build_evidence(conn, prediction: dict, *, include_raw: bool) -> dict:
    snap = conn.execute(select(snapshots)
                        .where(snapshots.c.snapshot_id == prediction["snapshot_id"])).mappings().first()
    version = conn.execute(select(versions)
                           .where(versions.c.model_version_id == prediction["model_version_id"])).mappings().first()
    # 窗口由模型版本与受理时间（向下取整到分钟）唯一确定，可据此关联该评分所在窗口的告警。
    window_start, _ = window_bounds(prediction["request_received_at"])
    window_id = str(uuid5(NAMESPACE_URL,
                          f"financial-ai:window:{prediction['model_version_id']}:{window_start}"))
    alert_rows = conn.execute(select(alerts).where(alerts.c.window_id == window_id)).mappings().all()
    alert_ids = [row["alert_id"] for row in alert_rows]
    actions = []
    if alert_ids:
        actions = conn.execute(select(alert_actions)
                               .where(alert_actions.c.alert_id.in_(alert_ids))
                               .order_by(alert_actions.c.created_at)).mappings().all()
    evidence = {
        "prediction_id": prediction["prediction_id"],
        "principal_id": prediction["principal_id"],
        "model_version_id": prediction["model_version_id"],
        "model_version": version["version"] if version else None,
        "model_id": version["model_id"] if version else None,
        "manifest_sha256": version["manifest_sha256"] if version else None,
        "status": prediction["status"],
        "risk_probability": prediction["risk_probability"],
        "error_code": prediction["error_code"],
        "inference_ms": prediction["inference_ms"],
        "inference_attempted": bool(prediction["inference_attempted"]),
        "request_received_at": prediction["request_received_at"],
        "completed_at": prediction["completed_at"],
        "http_status": prediction["http_status"],
        "explanation": prediction["explanation_snapshot"],
        "missing_fields": snap["missing_fields"] if snap else None,
        "input_sha256": snap["content_sha256"] if snap else None,
        "normalized_input": snap["normalized_input"] if snap else None,
        "window_id": window_id,
        "alerts": [{"alert_id": row["alert_id"], "metric_name": row["metric_name"],
                    "feature": row["feature"], "status": row["status"], "value": row["value"]}
                   for row in alert_rows],
        "alert_actions": [{"alert_id": row["alert_id"], "principal_id": row["principal_id"],
                           "from_status": row["from_status"], "to_status": row["to_status"],
                           "note": row["note"], "created_at": row["created_at"]} for row in actions],
    }
    response = prediction["response_json"] or {}
    evidence["decision_threshold"] = response.get("decision_threshold")
    evidence["decision"] = response.get("decision")
    if include_raw:
        evidence["original_input"] = snap["original_input"] if snap else None
    else:
        evidence["original_input"] = None
        evidence["raw_input_withheld"] = True
    return evidence


def record_query(engine, actor, prediction_id: str, action: str, details: dict) -> None:
    with engine.begin() as conn:
        conn.execute(insert(audit_logs).values(
            audit_id=str(uuid4()), principal_id=actor.principal_id, action=action,
            resource_id=prediction_id, created_at=stamp(datetime.now(timezone.utc)),
            details=details))


def search(engine, actor, *, model_version_id, start, end, status, offset, limit) -> dict:
    try:
        start_dt, end_dt = parse_time(start), parse_time(end)
    except (ValueError, TypeError):
        raise DomainError(422, "INVALID_RANGE", "from and to must be ISO 8601 timestamps") from None
    if end_dt <= start_dt:
        raise DomainError(422, "INVALID_RANGE", "to must be later than from")
    if (end_dt - start_dt).days > MAX_RANGE_DAYS:
        raise DomainError(422, "RANGE_TOO_WIDE", f"time range must not exceed {MAX_RANGE_DAYS} days")
    conditions = [predictions.c.request_received_at >= stamp(start_dt),
                  predictions.c.request_received_at < stamp(end_dt)]
    if model_version_id:
        model_id = model_id_of(engine.connect(), model_version_id)
        if model_id is None:
            raise DomainError(404, "VERSION_NOT_FOUND", "model version does not exist")
        require_scope(actor, model_id)
        conditions.append(predictions.c.model_version_id == model_version_id)
    if status:
        if status not in {"success", "failed"}:
            raise DomainError(422, "INVALID_STATUS", "status must be success or failed")
        conditions.append(predictions.c.status == status)
    with engine.connect() as conn:
        allowed = list(actor.model_ids)
        if not allowed:
            raise DomainError(403, "FORBIDDEN", "no model authorization")
        joined = predictions.join(versions, predictions.c.model_version_id == versions.c.model_version_id)
        scope = versions.c.model_id.in_(allowed)
        rows = conn.execute(select(predictions).select_from(joined)
                            .where(and_(*conditions, scope))
                            .order_by(predictions.c.request_received_at.desc(),
                                      predictions.c.prediction_id)
                            .limit(limit).offset(offset)).mappings().all()
    return {"items": [{
        "prediction_id": r["prediction_id"],
        "model_version_id": r["model_version_id"],
        "status": r["status"],
        "risk_probability": r["risk_probability"],
        "request_received_at": r["request_received_at"],
        "inference_ms": r["inference_ms"],
    } for r in rows], "offset": offset, "limit": limit}


def export(engine, actor, prediction_id: str, fmt: str) -> tuple[bytes, str, dict]:
    with engine.connect() as conn:
        prediction = load_prediction(conn, prediction_id)
        model_id = model_id_of(conn, prediction["model_version_id"])
        require_scope(actor, model_id)
        include_raw = bool(FULL_VIEW_ROLES & set(actor.roles))
        evidence = build_evidence(conn, prediction, include_raw=include_raw)
    evidence["exported_at"] = stamp(datetime.now(timezone.utc))
    if fmt == "csv":
        flat = {k: (json.dumps(v, ensure_ascii=False, sort_keys=True) if isinstance(v, (dict, list)) else v)
                for k, v in evidence.items()}
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=list(flat))
        writer.writeheader()
        writer.writerow(flat)
        payload = ("﻿" + buffer.getvalue()).encode("utf-8")
        media = "text/csv; charset=utf-8"
    else:
        payload = json.dumps(evidence, ensure_ascii=False, indent=2).encode("utf-8")
        media = "application/json; charset=utf-8"
    digest = hashlib.sha256(payload).hexdigest()
    with engine.begin() as conn:
        conn.execute(insert(audit_exports).values(
            export_id=str(uuid4()), prediction_id=prediction_id, format=fmt,
            file_sha256=digest, requested_by=actor.principal_id,
            created_at=evidence["exported_at"]))
    record_query(engine, actor, prediction_id, "audit.export",
                 {"format": fmt, "file_sha256": digest})
    return payload, media, {"file_sha256": digest, "format": fmt}
