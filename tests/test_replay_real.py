"""B4 复现的真实推理回归：使用固定版本模型包，不使用桩。

本文件要求运行环境与模型包 environment.json 一致（CPython 3.12.14 / Windows AMD64）。
"""

import json
import os
import tempfile
import unittest

from fastapi.testclient import TestClient

from app.auth.store import AuthStore
from app.config import Settings
from app.demo_auth import DEMO_MODEL_ID
from app.main import create_app

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class RealReplayTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory(dir=os.path.dirname(__file__), prefix=".b4-real-")
        self.addCleanup(self.dir.cleanup)
        settings = Settings(database_url="sqlite:///" + os.path.join(self.dir.name, "real.db"),
                            admin_token="a" * 40, scorer_token="b" * 40,
                            jwt_secret="s" * 40, token_ttl_minutes=30,
                            artifact_root=os.path.join(ROOT, "artifacts"))
        self.app = create_app(settings)
        self.client = self.enterContext(TestClient(self.app))
        auth = AuthStore(self.app.state.store.engine)
        auth.seed_roles()
        auth.create_user("admin", "password123", "管理员", ["admin"], [DEMO_MODEL_ID])
        auth.create_user("auditor", "password123", "审计", ["auditor"], [DEMO_MODEL_ID])
        self.admin = self.headers("admin")
        self.auditor = self.headers("auditor")
        self.version_id = self.client.get("/api/v1/model-versions", headers=self.admin).json()["items"][0]["model_version_id"]
        self.client.post(f"/api/v1/model-versions/{self.version_id}/activate",
                         headers={**self.admin, "Idempotency-Key": "activate-1"})
        with open(os.path.join(ROOT, "examples", "A2-score-input.json"), encoding="utf-8") as handle:
            self.raw = json.load(handle)
        response = self.client.post("/api/v1/predictions",
                                    json={"model_version_id": self.version_id, "input": self.raw},
                                    headers={**self.admin, "Idempotency-Key": "score-1"})
        self.assertEqual(201, response.status_code)
        self.prediction = response.json()

    def headers(self, username):
        token = self.client.post("/api/v1/auth/token",
                                 json={"username": username, "password": "password123"}).json()["access_token"]
        return {"Authorization": "Bearer " + token}

    def test_replay_reproduces_probability_with_same_package(self):
        created = self.client.post("/api/v1/replay-runs",
                                   json={"prediction_id": self.prediction["prediction_id"]},
                                   headers={**self.auditor, "Idempotency-Key": "replay-1"})
        self.assertEqual(201, created.status_code)
        self.assertEqual(1, self.app.state.replay.process_pending()["passed"])
        body = self.client.get(f"/api/v1/replay-runs/{created.json()['replay_id']}",
                               headers=self.auditor).json()
        self.assertEqual("passed", body["status"])
        self.assertEqual("verified", body["environment_check"])
        self.assertLessEqual(abs(body["absolute_error"]), 1e-8)
        self.assertEqual(1, body["decision_match"])
        self.assertAlmostEqual(self.prediction["risk_probability"], body["replay_probability"], places=10)

    def test_evidence_matches_the_scored_input(self):
        body = self.client.get(f"/api/v1/predictions/{self.prediction['prediction_id']}",
                               headers=self.auditor).json()
        self.assertEqual(self.raw, body["original_input"])
        self.assertEqual(self.version_id, body["model_version_id"])
        self.assertIsNotNone(body["explanation"])


if __name__ == "__main__":
    unittest.main()
