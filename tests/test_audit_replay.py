"""B3 审计查询与 B4 历史复现的接口测试。

复现执行通过替换 loader/scorer 注入，不依赖模型包与本机 Python 版本。
"""

import hashlib
import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timezone
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import insert, select

from app.auth.store import AuthStore
from app.config import Settings
from app.db.schema import audit_exports, replay_runs
from app.demo_auth import DEMO_MODEL_ID
from app.main import create_app
from app.modules.monitoring.time_utils import stamp
from app.storage.sqlite_store import models, predictions, snapshots, versions

VERSION_ID = "11111111-1111-1111-1111-111111111111"
OTHER_MODEL = "33333333-3333-3333-3333-333333333333"
OTHER_VERSION = "44444444-4444-4444-4444-444444444444"
PASSWORD = "password123"
RECEIVED = stamp(datetime(2026, 11, 12, 10, 0, 5, tzinfo=timezone.utc))


class StubPredictor:
    def descriptors(self):
        return [{"model_version_id": VERSION_ID, "model_id": DEMO_MODEL_ID,
                 "version": "v-test", "manifest_sha256": "0" * 64}]

    def score(self, *args, **kwargs):  # pragma: no cover
        raise RuntimeError("not used")


class AuditReplayBase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory(dir=os.path.dirname(__file__), prefix=".b3-test-")
        self.addCleanup(self.dir.cleanup)
        settings = Settings(database_url="sqlite:///" + os.path.join(self.dir.name, "audit.db"),
                            admin_token="a" * 40, scorer_token="b" * 40,
                            jwt_secret="s" * 40, token_ttl_minutes=30)
        self.app = create_app(settings, predictor=StubPredictor())
        self.client = self.enterContext(TestClient(self.app))
        self.engine = self.app.state.store.engine
        auth = AuthStore(self.engine)
        auth.seed_roles()
        auth.create_user("auditor", PASSWORD, "审计", ["auditor"], [DEMO_MODEL_ID])
        auth.create_user("risk", PASSWORD, "风险", ["risk_manager"], [DEMO_MODEL_ID])
        with self.engine.begin() as conn:
            conn.execute(insert(models).values(model_id=OTHER_MODEL, name="未授权模型"))
            conn.execute(insert(versions).values(
                model_version_id=OTHER_VERSION, model_id=OTHER_MODEL, version="v-other",
                manifest_sha256="1" * 64, status="validated"))
        self.prediction_id = self.make_prediction(str(uuid4()), VERSION_ID, probability=0.42)
        self.other_prediction = self.make_prediction(str(uuid4()), OTHER_VERSION, probability=0.7)

    def make_prediction(self, prediction_id, version_id, *, probability, status="success"):
        snapshot_id = str(uuid4())
        with self.engine.begin() as conn:
            conn.execute(insert(snapshots).values(
                snapshot_id=snapshot_id, original_input={"age": 30, "monthly_income": "8000.00"},
                normalized_input={"age": 30}, missing_fields=[], content_sha256="c" * 64))
            conn.execute(insert(predictions).values(
                prediction_id=prediction_id, principal_id="caller", route="/api/v1/predictions",
                idempotency_key=str(uuid4()), request_sha256="r" * 64,
                model_version_id=version_id, snapshot_id=snapshot_id, status=status,
                risk_probability=probability, error_code=None if status == "success" else "INFERENCE_FAILED",
                inference_ms=3.5, inference_attempted=True, request_received_at=RECEIVED,
                completed_at=RECEIVED, explanation_snapshot={"top_factors": []}, http_status=201,
                response_json={"decision": 0 if probability < 0.5 else 1, "decision_threshold": 0.5}))
        return prediction_id

    def headers(self, username):
        token = self.client.post("/api/v1/auth/token",
                                 json={"username": username, "password": PASSWORD}).json()["access_token"]
        return {"Authorization": "Bearer " + token}


class EvidenceTests(AuditReplayBase):
    def test_auditor_sees_raw_input_and_evidence_chain(self):
        response = self.client.get(f"/api/v1/predictions/{self.prediction_id}",
                                   headers=self.headers("auditor"))
        self.assertEqual(200, response.status_code)
        body = response.json()
        self.assertEqual({"age": 30, "monthly_income": "8000.00"}, body["original_input"])
        self.assertEqual("v-test", body["model_version"])
        self.assertEqual(0.42, body["risk_probability"])
        self.assertEqual(0.5, body["decision_threshold"])
        self.assertIn("window_id", body)

    def test_other_roles_get_reduced_view(self):
        response = self.client.get(f"/api/v1/predictions/{self.prediction_id}",
                                   headers=self.headers("risk"))
        self.assertEqual(200, response.status_code)
        self.assertIsNone(response.json()["original_input"])
        self.assertTrue(response.json()["raw_input_withheld"])

    def test_model_outside_scope_is_forbidden(self):
        response = self.client.get(f"/api/v1/predictions/{self.other_prediction}",
                                   headers=self.headers("auditor"))
        self.assertEqual(403, response.status_code)

    def test_missing_prediction_is_404(self):
        response = self.client.get("/api/v1/predictions/55555555-5555-5555-5555-555555555555",
                                   headers=self.headers("auditor"))
        self.assertEqual(404, response.status_code)


class SearchTests(AuditReplayBase):
    RANGE = {"from": "2026-11-12T00:00:00Z", "to": "2026-11-13T00:00:00Z"}

    def test_search_returns_authorized_records_only(self):
        response = self.client.get("/api/v1/audit/records", params=self.RANGE,
                                   headers=self.headers("auditor"))
        self.assertEqual(200, response.status_code)
        ids = [item["prediction_id"] for item in response.json()["items"]]
        self.assertEqual([self.prediction_id], ids)

    def test_time_range_is_mandatory_and_bounded(self):
        headers = self.headers("auditor")
        self.assertEqual(422, self.client.get("/api/v1/audit/records", headers=headers).status_code)
        too_wide = {"from": "2026-01-01T00:00:00Z", "to": "2026-03-01T00:00:00Z"}
        self.assertEqual(422, self.client.get("/api/v1/audit/records", params=too_wide,
                                              headers=headers).status_code)

    def test_limit_above_100_is_rejected(self):
        params = {**self.RANGE, "limit": 101}
        self.assertEqual(422, self.client.get("/api/v1/audit/records", params=params,
                                              headers=self.headers("auditor")).status_code)

    def test_risk_manager_cannot_search(self):
        self.assertEqual(403, self.client.get("/api/v1/audit/records", params=self.RANGE,
                                              headers=self.headers("risk")).status_code)

    def test_range_rejects_31_days_plus_one_second(self):
        response = self.client.get("/api/v1/audit/records",
            params={"from": "2026-01-01T00:00:00Z", "to": "2026-02-01T00:00:01Z"},
            headers=self.headers("auditor"))
        self.assertEqual(422, response.status_code)

    def test_version_search_releases_connection(self):
        for _ in range(20):
            response = self.client.get("/api/v1/audit/records",
                params={**self.RANGE, "model_version_id": VERSION_ID}, headers=self.headers("auditor"))
            self.assertEqual(200, response.status_code)
        self.assertEqual(0, self.engine.pool.checkedout())

    def test_status_filter(self):
        response = self.client.get("/api/v1/audit/records", params={**self.RANGE, "status": "failed"},
                                   headers=self.headers("auditor"))
        self.assertEqual(200, response.status_code)
        self.assertEqual([], response.json()["items"])


class ExportTests(AuditReplayBase):
    def test_json_export_matches_database_and_is_recorded(self):
        response = self.client.get(f"/api/v1/audit/records/{self.prediction_id}/export",
                                   headers=self.headers("auditor"))
        self.assertEqual(200, response.status_code)
        digest = response.headers["X-File-SHA256"]
        self.assertEqual(hashlib.sha256(response.content).hexdigest(), digest)
        payload = json.loads(response.content)
        self.assertEqual(self.prediction_id, payload["prediction_id"])
        self.assertIn("exported_at", payload)
        with self.engine.connect() as conn:
            row = conn.execute(select(audit_exports)
                               .where(audit_exports.c.prediction_id == self.prediction_id)).mappings().first()
        self.assertEqual(digest, row["file_sha256"])

    def test_csv_export_has_bom_and_format(self):
        response = self.client.get(f"/api/v1/audit/records/{self.prediction_id}/export",
                                   params={"format": "csv"}, headers=self.headers("auditor"))
        self.assertEqual(200, response.status_code)
        self.assertTrue(response.content.startswith(b"\xef\xbb\xbf"))
        self.assertIn("prediction_id", response.content.decode("utf-8-sig").splitlines()[0])

    def test_export_of_out_of_scope_record_is_forbidden(self):
        response = self.client.get(f"/api/v1/audit/records/{self.other_prediction}/export",
                                   headers=self.headers("auditor"))
        self.assertEqual(403, response.status_code)


class ReplayTests(AuditReplayBase):
    def test_concurrent_same_key_returns_one_task(self):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: self.create(key="same-concurrent-key"), range(2)))
        self.assertEqual([201, 201], [r.status_code for r in responses])
        self.assertEqual(responses[0].json()["replay_id"], responses[1].json()["replay_id"])

    def test_same_key_returns_task_after_completion(self):
        first = self.create().json()
        service = self.use_stub_runner(lambda bundle, raw: {"risk_probability": 0.42, "decision": 0})
        service.process_pending()
        repeated = self.create()
        self.assertEqual(201, repeated.status_code)
        self.assertEqual(first["replay_id"], repeated.json()["replay_id"])
        self.assertEqual("passed", repeated.json()["status"])

    def test_same_key_other_prediction_conflicts(self):
        self.create()
        another = self.make_prediction(str(uuid4()), VERSION_ID, probability=0.4)
        response = self.client.post("/api/v1/replay-runs", json={"prediction_id": another},
            headers={**self.headers("auditor"), "Idempotency-Key": "key-1"})
        self.assertEqual(409, response.status_code)
        self.assertEqual("IDEMPOTENCY_CONFLICT", response.json()["error"]["code"])

    def test_restart_recovers_claimed_task(self):
        task = self.create().json()
        service = self.use_stub_runner(lambda bundle, raw: {"risk_probability": 0.42, "decision": 0})
        self.assertEqual(task["replay_id"], service._claim()["replay_id"])
        self.assertEqual(1, service.recover_interrupted())
        self.assertEqual(0, service.recover_interrupted())
        self.assertEqual(1, service.process_pending()["passed"])
        with self.engine.connect() as conn:
            from app.storage.sqlite_store import audit_logs
            rows = conn.execute(select(audit_logs).where(audit_logs.c.action == "replay.recovered")).all()
        self.assertEqual(1, len(rows))

    def create(self, headers=None, key="key-1"):
        return self.client.post("/api/v1/replay-runs", json={"prediction_id": self.prediction_id},
                                headers={**(headers or self.headers("auditor")), "Idempotency-Key": key})

    def test_create_and_duplicate(self):
        first = self.create()
        self.assertEqual(201, first.status_code)
        self.assertEqual("pending", first.json()["status"])
        self.assertEqual(self.prediction_id, first.json()["prediction_id"])
        self.assertEqual(VERSION_ID, first.json()["model_version_id"])
        self.assertEqual(409, self.create(key="key-2").status_code)

    def test_concurrent_creation_returns_one_conflict(self):
        from concurrent.futures import ThreadPoolExecutor
        service = self.app.state.replay
        original_clock = service.clock

        def slow_clock():
            # 放大“检查活动任务—插入”窗口，确保两个请求真正交错，避免测试因调度错开而空过。
            time.sleep(0.3)
            return original_clock()

        service.clock = slow_clock
        self.addCleanup(setattr, service, "clock", original_clock)
        headers = self.headers("auditor")
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda key: self.create(headers=headers, key=key), ["parallel-1", "parallel-2"]))
        self.assertEqual([201, 409], sorted(response.status_code for response in responses))
        with self.engine.connect() as conn:
            rows = conn.execute(select(replay_runs).where(replay_runs.c.prediction_id == self.prediction_id)).all()
        self.assertEqual(1, len(rows))

    def test_non_auditor_cannot_create(self):
        self.assertEqual(403, self.create(headers=self.headers("risk")).status_code)

    def use_stub_runner(self, scorer):
        service = self.app.state.replay
        service.loader = lambda *args: object()
        service.scorer = scorer
        return service

    def test_passed_run(self):
        replay_id = self.create().json()["replay_id"]
        service = self.use_stub_runner(lambda bundle, raw: {"risk_probability": 0.42, "decision": 0})
        self.assertEqual(1, service.process_pending()["passed"])
        body = self.client.get(f"/api/v1/replay-runs/{replay_id}", headers=self.headers("auditor")).json()
        self.assertEqual("passed", body["status"])
        self.assertEqual(0.42, body["replay_probability"])
        self.assertAlmostEqual(0.0, body["absolute_error"])
        self.assertEqual(1, body["decision_match"])

    def test_mismatch_run(self):
        replay_id = self.create().json()["replay_id"]
        service = self.use_stub_runner(lambda bundle, raw: {"risk_probability": 0.9, "decision": 1})
        self.assertEqual(1, service.process_pending()["mismatch"])
        body = self.client.get(f"/api/v1/replay-runs/{replay_id}", headers=self.headers("auditor")).json()
        self.assertEqual("mismatch", body["status"])
        self.assertAlmostEqual(0.48, body["absolute_error"], places=6)

    def test_package_failure_is_reported(self):
        replay_id = self.create().json()["replay_id"]

        def boom(*args):
            raise ValueError("environment mismatch; restore the pinned environment before loading")
        service = self.app.state.replay
        service.loader = boom
        self.assertEqual(1, service.process_pending()["failed"])
        body = self.client.get(f"/api/v1/replay-runs/{replay_id}", headers=self.headers("auditor")).json()
        self.assertEqual("failed", body["status"])
        self.assertEqual("environment_mismatch", body["environment_check"])

    def test_replay_uses_the_original_version(self):
        replay_id = self.create().json()["replay_id"]
        with self.engine.connect() as conn:
            row = conn.execute(select(replay_runs)
                               .where(replay_runs.c.replay_id == replay_id)).mappings().first()
        self.assertEqual(VERSION_ID, row["model_version_id"])


if __name__ == "__main__":
    unittest.main()
