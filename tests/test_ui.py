"""A5 HTTP boundary, identity, retry and real Streamlit widget acceptance."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
import httpx
from streamlit.testing.v1 import AppTest

from app.config import Settings
from app.main import create_app
from ui.api_client import ApiClient, ApiError, command_key

ROOT = Path(__file__).resolve().parents[1]
ADMIN = "a5-test-admin-" + "a" * 40
SCORER = "a5-test-scorer-" + "b" * 40


class ClientTests(unittest.TestCase):
    def test_auth_pagination_and_no_token_in_url(self):
        def handle(request):
            self.assertEqual(request.headers["Authorization"], "Bearer private-token")
            self.assertNotIn("private-token", str(request.url))
            self.assertEqual(request.url.params["offset"], "60")
            return httpx.Response(200, json={"items": [{"value": None}]})
        api = ApiClient("http://localhost:8000", "private-token", transport=httpx.MockTransport(handle))
        self.assertEqual(api.page("/api/v1/metrics", "version", 60), [{"value": None}])

    def test_post_requires_key_and_never_follows_redirect(self):
        calls = []
        def handle(request):
            calls.append(request)
            return httpx.Response(307, headers={"Location": "https://untrusted.example"})
        api = ApiClient("http://localhost", "secret", transport=httpx.MockTransport(handle))
        with self.assertRaises(ValueError):
            api.request("POST", "/api/v1/monitor-rules", body={})
        with self.assertRaises(ApiError):
            api.request("POST", "/api/v1/monitor-rules", body={}, key="stable-key")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].headers["Idempotency-Key"], "stable-key")

    def test_network_failure_is_sanitized_and_not_retried(self):
        calls = []
        def handle(request):
            calls.append(request)
            raise httpx.ConnectError("private-token / local-path", request=request)
        api = ApiClient("http://localhost", "private-token", transport=httpx.MockTransport(handle))
        with self.assertRaises(ApiError) as caught:
            api.request("POST", "/api/v1/monitor-rules", key="same", body={})
        self.assertNotIn("private-token", str(caught.exception))
        self.assertEqual(len(calls), 1)

    def test_server_errors_do_not_echo_raw_response(self):
        for status in (401, 403, 409, 422, 503):
            api = ApiClient("http://localhost", "secret", transport=httpx.MockTransport(
                lambda request: httpx.Response(status, text="secret traceback")))
            with self.assertRaises(ApiError) as caught:
                api.request("GET", "/api/v1/me")
            self.assertEqual(caught.exception.status, status)
            self.assertNotIn("secret", str(caught.exception))

    def test_invalid_json_reports_protocol_error(self):
        api = ApiClient("http://localhost", "secret", transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text="not json")))
        with self.assertRaises(ApiError) as caught:
            api.request("GET", "/api/v1/me")
        self.assertEqual(caught.exception.code, "INVALID_RESPONSE")

    def test_command_retry_reuses_key_and_changed_body_changes_key(self):
        state = {}
        first = command_key(state, "/rule", {"a": 1, "b": 2})
        self.assertEqual(first, command_key(state, "/rule", {"b": 2, "a": 1}))
        self.assertNotEqual(first, command_key(state, "/rule", {"a": 2, "b": 2}))
        self.assertNotEqual(first, command_key({}, "/rule", {"a": 1, "b": 2}))

    def test_unsafe_configuration_is_rejected(self):
        for url in ("file:///tmp/db", "http://user:secret@localhost", "http://localhost?token=secret"):
            with self.assertRaises(ValueError):
                ApiClient(url, "secret")


class IdentityTests(unittest.TestCase):
    def test_identity_is_authenticated_and_contains_no_credential(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "tests", prefix=".a5-test-") as folder:
            settings = Settings(database_url="sqlite:///" + (Path(folder) / "test.db").as_posix(),
                                admin_token=ADMIN, scorer_token=SCORER)
            with TestClient(create_app(settings)) as api:
                self.assertEqual(api.get("/api/v1/me").status_code, 401)
                for token, roles in ((ADMIN, ["admin", "scorer"]), (SCORER, ["scorer"])):
                    result = api.get("/api/v1/me", headers={"Authorization": "Bearer " + token})
                    self.assertEqual(result.status_code, 200)
                    self.assertEqual(result.json()["roles"], roles)
                    self.assertNotIn(token, result.text)


class WidgetTests(unittest.TestCase):
    def setUp(self):
        self.version = {"model_version_id": "ef31e86c-44a0-5925-bb71-26c41da39d32", "version": "credit-logreg-v1",
                        "active": False, "model_id": "model", "status": "validated", "manifest_sha256": "hash"}
        self.rule = {"rule_version": 1, "threshold": 0.05, "min_samples": 1}
        self.alert = {"alert_id": "39fc1056-e20b-44dd-91a9-2c972da9cb7a", "model_version_id": self.version["model_version_id"],
                      "metric_name": "missing_rate", "feature": "monthly_income", "value": 0.25,
                      "sample_count": 40, "condition_active": True, "status": "open", "window_id": "window",
                      "window_revision": 1, "rule_snapshot": self.rule, "assigned_to": None, "revisions": [], "actions": []}
        self.metric_rows = []
        self.calls = []
        self.fail = None

    def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        if self.fail:
            raise self.fail
        if method == "POST":
            return {"status": "saved"}
        if path.endswith("/me"):
            return {"principal_id": "actor", "roles": ["scorer"], "model_ids": ["model"]}
        if path.endswith("model-versions"):
            return {"items": [self.version]}
        if path.endswith("status"):
            return {"worker": {"state": "stale"}, "queue": {"pending": 4}, "failed_windows": 0}
        if "/alerts/" in path:
            return self.alert
        raise AssertionError(path)

    def page(self, path, *args, **kwargs):
        if path.endswith("/alerts"):
            return [self.alert]
        return self.metric_rows if path.endswith("metrics") else [self.rule]

    def run_page(self, name, roles=("scorer",)):
        script = f"from ui.pages.{name} import render\nrender()"
        app = AppTest.from_string(script, default_timeout=10)
        app.session_state["identity"] = {"principal_id": "actor", "roles": list(roles), "model_ids": ["model"]}
        app.session_state["token"] = "not-a-real-token"
        self.enterContext(patch("ui.common.client", return_value=self))
        # Page modules import the client function at module load time.
        self.enterContext(patch(f"ui.pages.{name}.client", return_value=self)) if name != "models" else None
        app.run()
        self.assertFalse(app.exception)
        return app

    def test_model_scorer_has_no_activation_button(self):
        app = self.run_page("models")
        self.assertNotIn("激活所选版本", [b.label for b in app.button])
        self.assertEqual(len(app.dataframe), 1)

    def test_activation_requires_confirmation_and_uses_api(self):
        app = self.run_page("models", ("admin",))
        app.button[-1].click().run()
        self.assertTrue(app.warning)
        self.assertFalse(any(c[0] == "POST" for c in self.calls))
        app.checkbox[0].check()
        app.button[-1].click().run()
        self.assertFalse(app.exception)
        self.assertTrue(any(c[0] == "POST" and c[1].endswith("activate") for c in self.calls))
        first_key = next(c[2]["key"] for c in self.calls if c[0] == "POST")
        app.button[-1].click().run()
        keys = [c[2]["key"] for c in self.calls if c[0] == "POST"]
        self.assertNotEqual(first_key, keys[-1], "An acknowledged command must not prevent a later intentional activation")

    def test_empty_monitoring_explains_worker_and_window(self):
        app = self.run_page("monitoring")
        self.assertTrue(any("窗口结束" in i.value for i in app.info))

    def test_null_metric_is_not_displayed_as_zero_and_stale_is_warned(self):
        self.metric_rows = [{"metric_name": "feature_psi", "feature": "age", "value": None, "unit": "index",
            "sample_count": 40, "status": "insufficient_samples", "stale": True, "window_start": "2026-11-05T00:00:00Z",
            "window_end": "2026-11-05T00:01:00Z", "window_revision": 1, "latest_window_revision": 2}]
        app = self.run_page("monitoring")
        self.assertTrue(app.warning)
        self.assertTrue(app.dataframe[0].value["数值"].isna().all())
        self.assertTrue(any("无可绘制" in i.value for i in app.info))

    def test_worker_staleness_is_visible_to_admin(self):
        app = self.run_page("monitoring", ("admin",))
        self.assertTrue(any("心跳已过期" in w.value for w in app.warning))

    def test_alert_evidence_and_readonly_role(self):
        app = self.run_page("alerts")
        self.assertTrue(any("阈值 > 0.05" in m.value for m in app.markdown))
        self.assertNotIn("认领告警", [b.label for b in app.button])

    def test_close_requires_note_and_persists_via_api(self):
        self.alert["status"] = "acknowledged"
        app = self.run_page("alerts", ("admin",))
        app.button[-1].click().run()
        self.assertTrue(app.warning)
        self.assertFalse(any(c[0] == "POST" for c in self.calls))
        app.text_area[0].input("已核查为合成数据缺失注入")
        app.button[-1].click().run()
        self.assertTrue(any(c[0] == "POST" and c[1].endswith("close") for c in self.calls))

    def test_closed_corrected_alert_has_no_reopen_button(self):
        self.alert.update(status="closed", condition_active=False)
        app = self.run_page("alerts", ("admin",))
        self.assertTrue(any("消退" in i.value for i in app.info))
        self.assertEqual([b.label for b in app.button], ["刷新数据"])

    def test_login_failure_does_not_keep_token(self):
        with patch("ui.common.client", return_value=self):
            app = AppTest.from_file(str(ROOT / "streamlit_app.py"), default_timeout=10).run()
            self.fail = ApiError(401, "UNAUTHENTICATED")
            app.text_input[0].input("do-not-retain")
            app.button[0].click().run()
            self.assertFalse(app.exception)
            self.assertNotIn("token", app.session_state)
            self.assertTrue(app.error)

    def test_entrypoint_login_and_navigation_render(self):
        with patch("ui.common.client", return_value=self):
            app = AppTest.from_file(str(ROOT / "streamlit_app.py"), default_timeout=10).run()
            app.text_input[0].input("local-test-token")
            app.button[0].click().run()
            self.assertFalse(app.exception)
            self.assertEqual(app.title[0].value, "模型版本")
            self.assertEqual(app.session_state["identity"]["roles"], ["scorer"])
