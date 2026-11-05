"""A5 API factory: uvicorn app.main:create_app --factory --host 127.0.0.1."""

from contextlib import asynccontextmanager
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field

from app.config import Settings
from app.contracts import DomainError
from app.demo_auth import DemoTokenAuthenticator
from app.modules.scoring.service import LocalModelPredictor, PredictionService
from app.modules.monitoring.routes import attach_routes
from app.modules.monitoring.store import MonitoringStore
from app.storage.sqlite_store import SqlitePredictionStore


class PredictionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_version_id: UUID
    input: dict[str, Any] = Field(max_length=7)


def create_app(settings: Settings | None = None, *, store=None, authenticator=None, predictor=None, monitoring=None):
    settings = settings or Settings.from_env()
    authenticator = authenticator or DemoTokenAuthenticator(settings.admin_token, settings.scorer_token)
    store = store or SqlitePredictionStore(settings.database_url)
    predictor = predictor or LocalModelPredictor(settings.artifact_root, settings.inference_timeout_ms)
    service = PredictionService(store, predictor)
    monitoring = monitoring or (MonitoringStore(store) if isinstance(store, SqlitePredictionStore) else None)

    @asynccontextmanager
    async def lifespan(_app):
        try:
            store.initialize(predictor.descriptors())
            if monitoring is not None:
                monitoring.initialize()
            yield
        finally:
            store.close()

    app = FastAPI(title="Financial AI Risk Platform A5", version="0.5.0", lifespan=lifespan)
    app.state.store, app.state.service, app.state.predictor = store, service, predictor
    app.state.monitoring = monitoring
    bearer = HTTPBearer(auto_error=False)

    def principal(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]):
        if credentials is None:
            raise DomainError(401, "UNAUTHENTICATED", "A valid bearer token is required.")
        return authenticator.authenticate(credentials.credentials)

    def idempotency_key(value: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128,
                                                    pattern=r"^[A-Za-z0-9._:-]+$")]):
        return value

    @app.exception_handler(DomainError)
    async def domain_error(_request: Request, exc: DomainError):
        headers = {"WWW-Authenticate": "Bearer"} if exc.status == 401 else None
        return JSONResponse(status_code=exc.status, content={"error": {"code": exc.code, "message": exc.message}}, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request: Request, _exc: RequestValidationError):
        return JSONResponse(status_code=422, content={"error": {"code": "INVALID_REQUEST",
                            "message": "Request fields, version UUID or idempotency key are invalid."}})

    def response(result):
        return JSONResponse(status_code=result.http_status, content=result.body,
                            headers={"Idempotency-Replayed": str(result.replayed).lower()})

    @app.get("/health")
    def health():
        ok = store.healthy()
        return JSONResponse(status_code=200 if ok else 503, content={"status": "ok" if ok else "unavailable",
                            "database": "reachable" if ok else "unavailable"})

    @app.get("/api/v1/me")
    def current_identity(actor=Depends(principal)):
        return {"principal_id": actor.principal_id, "roles": sorted(actor.roles),
                "model_ids": sorted(actor.model_ids)}

    @app.get("/api/v1/model-versions")
    def list_versions(actor=Depends(principal)):
        return {"items": store.versions(actor.model_ids)}

    @app.post("/api/v1/model-versions/{version_id}/activate")
    def activate(version_id: UUID, actor=Depends(principal), key=Depends(idempotency_key)):
        return response(service.activate(actor, str(version_id), key))

    @app.post("/api/v1/predictions")
    def predict(body: PredictionRequest, actor=Depends(principal), key=Depends(idempotency_key)):
        return response(service.predict(actor, str(body.model_version_id), body.input, key))

    attach_routes(app, principal, idempotency_key, store, monitoring)
    return app
