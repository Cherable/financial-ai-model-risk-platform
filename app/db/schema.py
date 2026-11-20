"""B 的身份、审计与复现表，与 A 的表共享同一 metadata。

与 app/modules/monitoring/schema.py 采用同一模式：所有表挂在
app.storage.sqlite_store.metadata 上，由现有 initialize() 的 create_all 一并建出。
只新增表，不修改 A 的任何表结构，旧库可直接升级。
"""

from sqlalchemy import (Boolean, Column, Float, ForeignKey, Index, Integer, String,
                        Table, UniqueConstraint)

from app.storage.sqlite_store import metadata

# 角色是固定枚举，不做用户自定义角色。
ROLE_CODES = ("model_developer", "risk_manager", "auditor", "admin")

users = Table(
    "users", metadata,
    Column("user_id", String, primary_key=True),
    Column("username", String, nullable=False),
    Column("password_hash", String, nullable=False),
    Column("display_name", String, nullable=False),
    Column("status", String, nullable=False),
    Column("created_at", String, nullable=False),
    UniqueConstraint("username", name="uq_users_username"),
)

roles = Table(
    "roles", metadata,
    Column("role_code", String, primary_key=True),
    Column("description", String, nullable=False),
)

user_roles = Table(
    "user_roles", metadata,
    Column("user_id", ForeignKey("users.user_id"), primary_key=True),
    Column("role_code", ForeignKey("roles.role_code"), primary_key=True),
)

# 授权粒度到 model_id：一次授权覆盖该模型的后续全部版本。
model_access = Table(
    "model_access", metadata,
    Column("user_id", ForeignKey("users.user_id"), primary_key=True),
    Column("model_id", ForeignKey("models.model_id"), primary_key=True),
    Column("granted_by", String, nullable=False),
    Column("granted_at", String, nullable=False),
)

# 复现任务：由 API 创建，单 worker 分派执行。
replay_runs = Table(
    "replay_runs", metadata,
    Column("replay_id", String, primary_key=True),
    Column("prediction_id", ForeignKey("predictions.prediction_id"), nullable=False),
    Column("model_version_id", ForeignKey("model_versions.model_version_id"), nullable=False),
    Column("environment_check", String, nullable=False),
    Column("original_probability", Float),
    Column("replay_probability", Float),
    Column("absolute_error", Float),
    Column("decision_match", Boolean),
    Column("status", String, nullable=False),
    Column("failure_reason", String),
    Column("requested_by", String, nullable=False),
    Column("created_at", String, nullable=False),
    Column("finished_at", String),
)

# 同一评分不允许有两条未完成的任务，避免重复排队。
Index("ux_replay_runs_active", replay_runs.c.prediction_id, unique=True,
      sqlite_where=replay_runs.c.status.in_(("pending", "running")))
Index("ix_replay_runs_status", replay_runs.c.status, replay_runs.c.created_at)

# 导出粒度是单条评分证据，按 docs/04 冻结的接口。
audit_exports = Table(
    "audit_exports", metadata,
    Column("export_id", String, primary_key=True),
    Column("prediction_id", ForeignKey("predictions.prediction_id"), nullable=False),
    Column("format", String, nullable=False),
    Column("file_sha256", String, nullable=False),
    Column("requested_by", String, nullable=False),
    Column("created_at", String, nullable=False),
)

Index("ix_model_access_user", model_access.c.user_id)
