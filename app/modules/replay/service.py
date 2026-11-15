"""历史评分复现：任务创建与执行。

复现固定使用原评分记录的模型版本与包摘要，禁止回退到当前激活版本。
计算直接复用 A 的 load_bundle 与 score_one，不复刻推理逻辑。
"""

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import insert, select, update

from app.contracts import DomainError
from app.modules.models.bundle import load_bundle
from app.modules.monitoring.time_utils import now_utc, stamp
from app.modules.scoring.predict import score_one
from app.storage.sqlite_store import audit_logs, predictions, snapshots, versions
from app.db.schema import replay_runs

TOLERANCE = 1e-8
REPLAY_ROLES = {"auditor", "admin"}


def classify(exc: Exception) -> tuple[str, str]:
    text = str(exc)
    if isinstance(exc, FileNotFoundError):
        return "package_missing", text or "model package file is missing"
    if text.startswith("environment mismatch"):
        return "environment_mismatch", text
    if "digest" in text.lower() or "sha256" in text.lower():
        return "digest_mismatch", text
    return "inference_failed", text or exc.__class__.__name__


class ReplayService:
    def __init__(self, engine, artifact_root, *, loader=load_bundle, scorer=score_one,
                 clock=now_utc):
        self.engine, self.artifact_root, self.loader, self.scorer, self.clock = (
            engine, artifact_root, loader, scorer, clock)

    # ---------- 创建与查询 ----------
    def create(self, actor, prediction_id: str) -> dict:
        if not (REPLAY_ROLES & set(actor.roles)):
            raise DomainError(403, "FORBIDDEN", "Auditor or administrator role required.")
        with self.engine.begin() as conn:
            conn.exec_driver_sql("BEGIN IMMEDIATE")
            prediction = conn.execute(select(predictions)
                                      .where(predictions.c.prediction_id == prediction_id)).mappings().first()
            if prediction is None:
                raise DomainError(404, "PREDICTION_NOT_FOUND", "prediction does not exist")
            model_id = conn.execute(select(versions.c.model_id)
                                    .where(versions.c.model_version_id == prediction["model_version_id"])).scalar()
            if model_id not in actor.model_ids:
                raise DomainError(403, "FORBIDDEN", "Model is outside the authorized range.")
            active = conn.execute(select(replay_runs.c.replay_id)
                                  .where(replay_runs.c.prediction_id == prediction_id,
                                         replay_runs.c.status.in_(("pending", "running")))).first()
            if active is not None:
                raise DomainError(409, "REPLAY_ALREADY_RUNNING", "a replay task is already pending")
            replay_id = str(uuid4())
            conn.execute(insert(replay_runs).values(
                replay_id=replay_id, prediction_id=prediction_id,
                model_version_id=prediction["model_version_id"], environment_check="pending",
                status="pending", requested_by=actor.principal_id,
                created_at=stamp(self.clock())))
            conn.execute(insert(audit_logs).values(
                audit_id=str(uuid4()), principal_id=actor.principal_id, action="replay.requested",
                resource_id=replay_id, created_at=stamp(self.clock()),
                details={"prediction_id": prediction_id}))
        return self.get(actor, replay_id)

    def get(self, actor, replay_id: str) -> dict:
        with self.engine.connect() as conn:
            row = conn.execute(select(replay_runs)
                               .where(replay_runs.c.replay_id == replay_id)).mappings().first()
            if row is None:
                raise DomainError(404, "REPLAY_NOT_FOUND", "replay task does not exist")
            model_id = conn.execute(select(versions.c.model_id)
                                    .where(versions.c.model_version_id == row["model_version_id"])).scalar()
        if model_id not in actor.model_ids:
            raise DomainError(403, "FORBIDDEN", "Model is outside the authorized range.")
        return dict(row)

    # ---------- 执行 ----------
    def process_pending(self, limit: int = 5) -> dict:
        result = {"claimed": 0, "passed": 0, "mismatch": 0, "failed": 0, "skipped": 0}
        for _ in range(limit):
            claimed = self._claim()
            if claimed is None:
                break
            result["claimed"] += 1
            outcome = self._run(claimed)
            result[outcome] += 1
        return result

    def _claim(self):
        with self.engine.begin() as conn:
            row = conn.execute(select(replay_runs)
                               .where(replay_runs.c.status == "pending")
                               .order_by(replay_runs.c.created_at)
                               .limit(1)).mappings().first()
            if row is None:
                return None
            changed = conn.execute(update(replay_runs)
                                   .where(replay_runs.c.replay_id == row["replay_id"],
                                          replay_runs.c.status == "pending")
                                   .values(status="running")).rowcount
            return dict(row) if changed else None

    def _run(self, task: dict) -> str:
        try:
            status, check, details = self._compute(task)
        except Exception as exc:                      # 任何未预期错误都必须落成 failed，不能卡在 running
            status, check = "failed", "internal_error"
            details = {"failure_reason": str(exc) or exc.__class__.__name__}
        values = {"status": status, "environment_check": check, "finished_at": stamp(self.clock()),
                  **details}
        with self.engine.begin() as conn:
            conn.execute(update(replay_runs)
                         .where(replay_runs.c.replay_id == task["replay_id"]).values(**values))
            conn.execute(insert(audit_logs).values(
                audit_id=str(uuid4()), principal_id=task["requested_by"],
                action="replay." + status, resource_id=task["replay_id"],
                created_at=stamp(self.clock()), details={"environment_check": check}))
        return status

    def _compute(self, task: dict):
        with self.engine.connect() as conn:
            prediction = conn.execute(select(predictions)
                                      .where(predictions.c.prediction_id == task["prediction_id"])
                                      ).mappings().first()
            snapshot = conn.execute(select(snapshots)
                                    .where(snapshots.c.snapshot_id == prediction["snapshot_id"])
                                    ).mappings().first()
            version = conn.execute(select(versions)
                                   .where(versions.c.model_version_id == task["model_version_id"])
                                   ).mappings().first()
        original_probability = prediction["risk_probability"]
        response = prediction["response_json"] or {}
        original_decision = response.get("decision")

        try:
            bundle = self.loader(version["version"], version["manifest_sha256"], self.artifact_root)
        except Exception as exc:
            check, reason = classify(exc)
            return "failed", check, {"failure_reason": reason}

        if prediction["status"] == "failed":
            try:
                self.scorer(bundle, snapshot["original_input"])
            except Exception:
                return "passed", "verified", {"failure_reason": "original failure reproduced"}
            return "mismatch", "verified", {"failure_reason": "original failure not reproduced"}

        result = self.scorer(bundle, snapshot["original_input"])
        new_probability = result["risk_probability"]
        error = abs(new_probability - original_probability)
        decision_match = result["decision"] == original_decision
        status = "passed" if error <= TOLERANCE and decision_match else "mismatch"
        return status, "verified", {
            "original_probability": original_probability,
            "replay_probability": new_probability,
            "absolute_error": error,
            "decision_match": decision_match,
        }
