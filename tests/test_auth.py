"""B2 身份与权限的接口测试。

不加载模型包：注入桩预测器，只验证认证、授权与审计。
"""

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.auth.store import AuthStore
from app.auth.tokens import issue_token
from app.config import Settings
from app.demo_auth import DEMO_MODEL_ID
from app.main import create_app
from app.storage.sqlite_store import audit_logs

VERSION_ID = "11111111-1111-1111-1111-111111111111"
SECRET = "s" * 40
PASSWORD = "password123"


class StubPredictor:
    def descriptors(self):
        return [{"model_version_id": VERSION_ID, "model_id": DEMO_MODEL_ID,
                 "version": "v-test", "manifest_sha256": "0" * 64}]

    def score(self, *args, **kwargs):  # pragma: no cover - 本文件不评分
        raise RuntimeError("not used")


class AuthTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory(dir=os.path.dirname(__file__), prefix=".b2-test-")
        self.addCleanup(self.dir.cleanup)
        self.settings = Settings(database_url="sqlite:///" + os.path.join(self.dir.name, "auth.db"),
                                 admin_token="a" * 40, scorer_token="b" * 40,
                                 jwt_secret=SECRET, token_ttl_minutes=30)
        self.app = create_app(self.settings, predictor=StubPredictor())
        self.client = self.enterContext(TestClient(self.app))
        self.auth = AuthStore(self.app.state.store.engine)
        self.auth.seed_roles()
        self.auditor_id = self.auth.create_user("auditor", PASSWORD, "审计", ["auditor"], [DEMO_MODEL_ID])
        self.admin_id = self.auth.create_user("admin", PASSWORD, "管理员", ["admin"], [DEMO_MODEL_ID])

    def login(self, username, password=PASSWORD):
        return self.client.post("/api/v1/auth/token", json={"username": username, "password": password})

    def token(self, username, password=PASSWORD):
        return self.login(username, password).json()["access_token"]

    def headers(self, username, password=PASSWORD):
        return {"Authorization": "Bearer " + self.token(username, password)}


class LoginTests(AuthTestCase):
    def test_login_returns_token_and_expiry(self):
        response = self.login("auditor")
        self.assertEqual(200, response.status_code)
        body = response.json()
        self.assertEqual("Bearer", body["token_type"])
        self.assertTrue(body["access_token"])
        self.assertTrue(body["expires_at"].endswith("Z"))

    def test_wrong_password_and_unknown_user_are_indistinguishable(self):
        wrong = self.login("auditor", "definitely-wrong")
        unknown = self.login("nobody", "definitely-wrong")
        self.assertEqual(401, wrong.status_code)
        self.assertEqual(401, unknown.status_code)
        self.assertEqual(wrong.json(), unknown.json())

    def test_login_is_audited_without_password(self):
        self.login("auditor")
        self.login("auditor", "bad")
        with self.app.state.store.engine.connect() as conn:
            rows = conn.execute(select(audit_logs).where(audit_logs.c.action.like("auth.login%"))
                                ).mappings().all()
        actions = sorted(row["action"] for row in rows)
        self.assertEqual(["auth.login", "auth.login_failed"], actions)
        for row in rows:
            self.assertNotIn(PASSWORD, str(row["details"]))
            self.assertNotIn("argon2", str(row["details"]).lower())

    def test_disabled_account_is_rejected(self):
        self.auth.set_status(self.auditor_id, "disabled")
        self.assertEqual(401, self.login("auditor").status_code)

    def test_token_of_disabled_account_stops_working(self):
        headers = self.headers("auditor")
        self.assertEqual(200, self.client.get("/api/v1/me", headers=headers).status_code)
        self.auth.set_status(self.auditor_id, "disabled")
        self.assertEqual(401, self.client.get("/api/v1/me", headers=headers).status_code)


class TokenTests(AuthTestCase):
    def test_missing_or_tampered_token_is_rejected(self):
        self.assertEqual(401, self.client.get("/api/v1/me").status_code)
        headers = self.headers("auditor")
        tampered = headers["Authorization"][:-2] + "xx"
        self.assertEqual(401, self.client.get("/api/v1/me", headers={"Authorization": tampered}).status_code)

    def test_expired_token_is_rejected(self):
        past = datetime.now(timezone.utc) - timedelta(hours=2)
        token, _ = issue_token(self.auditor_id, SECRET, 30, now=past)
        response = self.client.get("/api/v1/me", headers={"Authorization": "Bearer " + token})
        self.assertEqual(401, response.status_code)

    def test_token_signed_with_other_secret_is_rejected(self):
        token, _ = issue_token(self.auditor_id, "x" * 40, 30)
        self.assertEqual(401, self.client.get("/api/v1/me", headers={"Authorization": "Bearer " + token}).status_code)


class AuthorisationTests(AuthTestCase):
    def test_me_returns_server_side_roles_and_models(self):
        body = self.client.get("/api/v1/me", headers=self.headers("auditor")).json()
        self.assertEqual(["auditor"], body["roles"])
        self.assertEqual([DEMO_MODEL_ID], body["model_ids"])

    def test_roles_are_not_taken_from_the_token(self):
        # 令牌只含 user_id：即使伪造 roles 字段也不生效
        import jwt
        forged = jwt.encode({"sub": self.auditor_id, "roles": ["admin"],
                             "exp": int((datetime.now(timezone.utc) + timedelta(minutes=5)).timestamp())},
                            SECRET, algorithm="HS256")
        body = self.client.get("/api/v1/me", headers={"Authorization": "Bearer " + forged}).json()
        self.assertEqual(["auditor"], body["roles"])

    def test_auditor_cannot_manage_users(self):
        self.assertEqual(403, self.client.get("/api/v1/users", headers=self.headers("auditor")).status_code)

    def test_admin_can_manage_users_and_access(self):
        from sqlalchemy import insert
        from app.storage.sqlite_store import models
        other = "99999999-9999-9999-9999-999999999999"
        with self.app.state.store.engine.begin() as conn:
            conn.execute(insert(models).values(model_id=other, name="另一个模型"))
        headers = self.headers("admin")
        listing = self.client.get("/api/v1/users", headers=headers)
        self.assertEqual(200, listing.status_code)
        self.assertEqual(2, len(listing.json()["items"]))

        granted = self.client.post(f"/api/v1/users/{self.auditor_id}/access", headers=headers,
                                   json={"model_id": other, "action": "grant"})
        self.assertEqual(200, granted.status_code)
        self.assertIn(other, self.client.get("/api/v1/me", headers=self.headers("auditor")).json()["model_ids"])

        revoked = self.client.post(f"/api/v1/users/{self.auditor_id}/access", headers=headers,
                                   json={"model_id": other, "action": "revoke"})
        self.assertEqual(200, revoked.status_code)
        self.assertNotIn(other, self.client.get("/api/v1/me", headers=self.headers("auditor")).json()["model_ids"])

    def test_unknown_role_and_weak_password_are_rejected(self):
        from app.contracts import DomainError
        with self.assertRaises(DomainError):
            self.auth.create_user("x1", PASSWORD, "x", ["superuser"])
        with self.assertRaises(DomainError):
            self.auth.create_user("x2", "short", "x", ["auditor"])
        with self.assertRaises(DomainError):
            self.auth.create_user("auditor", PASSWORD, "dup", ["auditor"])
        with self.assertRaises(DomainError):
            self.auth.grant("nobody", DEMO_MODEL_ID, "admin")


class DemoModeTests(unittest.TestCase):
    """未配置 JWT_SECRET 时保持 A 的演示认证，且不注册登录端点。"""

    def test_demo_tokens_still_work(self):
        with tempfile.TemporaryDirectory(dir=os.path.dirname(__file__), prefix=".b2-demo-") as tmp:
            settings = Settings(database_url="sqlite:///" + os.path.join(tmp, "demo.db"),
                                admin_token="a" * 40, scorer_token="b" * 40)
            app = create_app(settings, predictor=StubPredictor())
            with TestClient(app) as client:
                headers = {"Authorization": "Bearer " + "a" * 40}
                self.assertEqual(200, client.get("/api/v1/me", headers=headers).status_code)
                self.assertEqual(404, client.post("/api/v1/auth/token",
                                                  json={"username": "x", "password": "y"}).status_code)


if __name__ == "__main__":
    unittest.main()
