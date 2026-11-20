"""登录与用户管理路由。

登录端点不要求 Bearer；其余端点以管理员角色为界，并在服务端再次校验。
"""

from typing import Literal

from fastapi import Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.auth.service import JwtAuthService
from app.contracts import DomainError
from app.db.schema import model_access, user_roles, users


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class AccessRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_id: str = Field(min_length=1, max_length=64)
    action: Literal["grant", "revoke"]


class StatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["active", "disabled"]


def _require_admin(actor):
    if "admin" not in actor.roles:
        raise DomainError(403, "FORBIDDEN", "Administrator role required.")


def attach_routes(app, service: JwtAuthService, principal_dep):
    store = service.store

    @app.post("/api/v1/auth/token")
    def login(body: LoginRequest):
        return service.login(body.username, body.password)

    @app.get("/api/v1/users")
    def list_users(actor=Depends(principal_dep)):
        _require_admin(actor)
        with store.engine.connect() as conn:
            rows = conn.execute(select(users)).mappings().all()
            role_rows = conn.execute(select(user_roles)).mappings().all()
            access_rows = conn.execute(select(model_access)).mappings().all()
        by_user: dict[str, list[str]] = {}
        for row in role_rows:
            by_user.setdefault(row["user_id"], []).append(row["role_code"])
        access_by_user: dict[str, list[str]] = {}
        for row in access_rows:
            access_by_user.setdefault(row["user_id"], []).append(row["model_id"])
        return {"items": [
            {"user_id": r["user_id"], "username": r["username"],
             "display_name": r["display_name"], "status": r["status"],
             "roles": sorted(by_user.get(r["user_id"], [])),
             "model_ids": sorted(access_by_user.get(r["user_id"], []))}
            for r in rows]}

    @app.post("/api/v1/users/{user_id}/access")
    def change_access(user_id: str, body: AccessRequest, actor=Depends(principal_dep)):
        _require_admin(actor)
        if body.action == "grant":
            store.grant(user_id, body.model_id, actor.principal_id)
        else:
            store.revoke(user_id, body.model_id)
        store.record_action(actor.principal_id, f"model_access.{body.action}", user_id,
                            {"model_id": body.model_id})
        return {"user_id": user_id, "model_id": body.model_id, "action": body.action}

    @app.post("/api/v1/users/{user_id}/status")
    def change_status(user_id: str, body: StatusRequest, actor=Depends(principal_dep)):
        _require_admin(actor)
        store.set_status(user_id, body.status)
        store.record_action(actor.principal_id, "user.status", user_id, {"status": body.status})
        return {"user_id": user_id, "status": body.status}
