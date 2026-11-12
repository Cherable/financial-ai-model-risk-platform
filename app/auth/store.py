"""用户、角色与模型授权的读写。

角色映射：存储四类业务角色，但 A 的接口层已有硬编码的角色判断
（评分要求 scorer、激活要求 admin）。为不修改 A 的权限逻辑，
本层在组装 Principal 时做能力扩展：
  admin            -> admin, scorer
  model_developer  -> model_developer, scorer
其余角色原样保留。业务角色仍是文档中的四类。
"""

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import delete, insert, select

from app.auth.passwords import hash_password, verify_password
from app.contracts import DomainError, Principal
from app.db.schema import model_access, roles, user_roles, users
from app.modules.monitoring.time_utils import stamp
from app.storage.sqlite_store import audit_logs, models

# 存储角色 -> 对外角色集合
ROLE_EXPANSION = {
    "admin": frozenset({"admin", "scorer"}),
    "model_developer": frozenset({"model_developer", "scorer"}),
    "risk_manager": frozenset({"risk_manager"}),
    "auditor": frozenset({"auditor"}),
}

SEEDED_ROLES = {
    "model_developer": "模型开发人员",
    "risk_manager": "风险管理人员",
    "auditor": "审计人员",
    "admin": "管理员",
}


def expand_roles(stored: set[str]) -> frozenset[str]:
    result: set[str] = set()
    for role in stored:
        result |= ROLE_EXPANSION.get(role, frozenset({role}))
    return frozenset(result)


class AuthStore:
    def __init__(self, engine):
        self.engine = engine

    # ---------- 读取 ----------
    def user_by_username(self, username: str) -> dict | None:
        with self.engine.connect() as conn:
            row = conn.execute(select(users).where(users.c.username == username)).mappings().first()
        return dict(row) if row else None

    def user(self, user_id: str) -> dict | None:
        with self.engine.connect() as conn:
            row = conn.execute(select(users).where(users.c.user_id == user_id)).mappings().first()
        return dict(row) if row else None

    def roles_of(self, user_id: str) -> set[str]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(user_roles.c.role_code)
                                .where(user_roles.c.user_id == user_id)).scalars().all()
        return set(rows)

    def model_ids_of(self, user_id: str) -> set[str]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(model_access.c.model_id)
                                .where(model_access.c.user_id == user_id)).scalars().all()
        return set(rows)

    def principal(self, user_id: str) -> Principal | None:
        """用户不存在、已停用或没有任何角色时返回 None。"""
        user = self.user(user_id)
        if not user or user["status"] != "active":
            return None
        stored = self.roles_of(user_id)
        if not stored:
            return None
        return Principal(principal_id=user_id, roles=expand_roles(stored),
                         model_ids=frozenset(self.model_ids_of(user_id)))

    # ---------- 写入 ----------
    def seed_roles(self) -> None:
        with self.engine.begin() as conn:
            existing = set(conn.execute(select(roles.c.role_code)).scalars().all())
            for code, name in SEEDED_ROLES.items():
                if code not in existing:
                    conn.execute(insert(roles).values(role_code=code, description=name))

    def create_user(self, username: str, password: str, display_name: str,
                    role_codes, model_ids=()) -> str:
        if not username or len(username) > 64:
            raise DomainError(422, "INVALID_USERNAME", "username must be 1-64 characters")
        if not password or len(password) < 8:
            raise DomainError(422, "WEAK_PASSWORD", "password must be at least 8 characters")
        unknown = set(role_codes) - set(SEEDED_ROLES)
        if not role_codes or unknown:
            raise DomainError(422, "INVALID_ROLE", "unknown or empty role set")
        user_id = str(uuid4())
        now = stamp(datetime.now(timezone.utc))
        with self.engine.begin() as conn:
            if conn.execute(select(users.c.user_id).where(users.c.username == username)).first():
                raise DomainError(409, "USERNAME_TAKEN", "username already exists")
            conn.execute(insert(users).values(
                user_id=user_id, username=username, password_hash=hash_password(password),
                display_name=display_name or username, status="active", created_at=now))
            for code in role_codes:
                conn.execute(insert(user_roles).values(user_id=user_id, role_code=code))
            for model_id in model_ids:
                conn.execute(insert(model_access).values(
                    user_id=user_id, model_id=model_id, granted_by="system", granted_at=now))
        return user_id

    def set_status(self, user_id: str, status: str) -> None:
        if status not in {"active", "disabled"}:
            raise DomainError(422, "INVALID_STATUS", "status must be active or disabled")
        with self.engine.begin() as conn:
            result = conn.execute(users.update().where(users.c.user_id == user_id)
                                  .values(status=status))
            if result.rowcount == 0:
                raise DomainError(404, "USER_NOT_FOUND", "user does not exist")

    def grant(self, user_id: str, model_id: str, granted_by: str) -> None:
        now = stamp(datetime.now(timezone.utc))
        with self.engine.begin() as conn:
            if conn.execute(select(users.c.user_id).where(users.c.user_id == user_id)).first() is None:
                raise DomainError(404, "USER_NOT_FOUND", "user does not exist")
            if conn.execute(select(models.c.model_id).where(models.c.model_id == model_id)).first() is None:
                raise DomainError(404, "MODEL_NOT_FOUND", "model does not exist")
            exists = conn.execute(select(model_access.c.model_id)
                                  .where(model_access.c.user_id == user_id,
                                         model_access.c.model_id == model_id)).first()
            if exists is None:
                conn.execute(insert(model_access).values(
                    user_id=user_id, model_id=model_id, granted_by=granted_by, granted_at=now))

    def revoke(self, user_id: str, model_id: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(delete(model_access).where(model_access.c.user_id == user_id,
                                                    model_access.c.model_id == model_id))

    # ---------- 审计 ----------
    def record_action(self, actor: str, action: str, resource_id: str, details: dict) -> None:
        with self.engine.begin() as conn:
            conn.execute(insert(audit_logs).values(
                audit_id=str(uuid4()), principal_id=actor, action=action,
                resource_id=resource_id, created_at=stamp(datetime.now(timezone.utc)),
                details=details))

    def record_login(self, actor: str, username: str, succeeded: bool, reason: str = "") -> None:
        details = {"username": username, "succeeded": succeeded}
        if reason:
            details["reason"] = reason
        with self.engine.begin() as conn:
            conn.execute(insert(audit_logs).values(
                audit_id=str(uuid4()), principal_id=actor,
                action="auth.login" if succeeded else "auth.login_failed",
                resource_id=username, created_at=stamp(datetime.now(timezone.utc)),
                details=details))


def check_password(user: dict, password: str) -> bool:
    return verify_password(user.get("password_hash", ""), password)
