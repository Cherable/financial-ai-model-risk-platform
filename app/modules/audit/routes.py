"""审计查询与导出路由。"""

from fastapi import Depends, Query, Response

from app.contracts import DomainError
from app.modules.audit.service import (FULL_VIEW_ROLES, build_evidence, export, load_prediction,
                                       model_id_of, record_query, require_scope, search)
from app.storage.sqlite_store import storage_errors


def _require_auditor(actor):
    if not (FULL_VIEW_ROLES & set(actor.roles)):
        raise DomainError(403, "FORBIDDEN", "Auditor or administrator role required.")


def attach_routes(app, principal_dep, engine):
    @app.get("/api/v1/predictions/{prediction_id}")
    @storage_errors
    def evidence(prediction_id: str, actor=Depends(principal_dep)):
        include_raw = bool(FULL_VIEW_ROLES & set(actor.roles))
        with engine.connect() as conn:
            prediction = load_prediction(conn, prediction_id)
            require_scope(actor, model_id_of(conn, prediction["model_version_id"]))
            body = build_evidence(conn, prediction, include_raw=include_raw)
        record_query(engine, actor, prediction_id, "audit.evidence_view",
                     {"include_raw": include_raw})
        return body

    @app.get("/api/v1/audit/records")
    @storage_errors
    def records(actor=Depends(principal_dep),
                model_version_id: str | None = Query(default=None),
                start: str = Query(alias="from"),
                end: str = Query(alias="to"),
                status: str | None = Query(default=None),
                offset: int = Query(0, ge=0),
                limit: int = Query(20, ge=1, le=100)):
        _require_auditor(actor)
        result = search(engine, actor, model_version_id=model_version_id, start=start, end=end,
                        status=status, offset=offset, limit=limit)
        record_query(engine, actor, model_version_id or "*", "audit.search",
                     {"from": start, "to": end, "status": status,
                      "offset": offset, "limit": limit})
        return result

    @app.get("/api/v1/audit/records/{prediction_id}/export")
    @storage_errors
    def export_record(prediction_id: str, actor=Depends(principal_dep),
                      format: str = Query(default="json", pattern="^(json|csv)$")):
        _require_auditor(actor)
        payload, media, meta = export(engine, actor, prediction_id, format)
        return Response(content=payload, media_type=media,
                        headers={"X-File-SHA256": meta["file_sha256"],
                                 "Content-Disposition": f'attachment; filename="{prediction_id}.{format}"'})
