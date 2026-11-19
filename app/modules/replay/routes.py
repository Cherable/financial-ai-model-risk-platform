"""复现任务路由。任务由单 worker 分派执行，接口只负责创建与查询。"""

from fastapi import Depends
from pydantic import BaseModel, ConfigDict
from uuid import UUID

from app.modules.replay.service import ReplayService
from app.storage.sqlite_store import storage_errors


class ReplayRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prediction_id: UUID


def attach_routes(app, principal_dep, idempotency_key, service: ReplayService):
    @app.post("/api/v1/replay-runs", status_code=201)
    @storage_errors
    def create(body: ReplayRequest, actor=Depends(principal_dep), key=Depends(idempotency_key)):
        return service.create(actor, str(body.prediction_id), key=key)

    @app.get("/api/v1/replay-runs/{replay_id}")
    @storage_errors
    def read(replay_id: str, actor=Depends(principal_dep)):
        return service.get(actor, replay_id)
