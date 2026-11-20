"""B5 审计与复现页面的界面测试。

沿用 test_ui.py 的做法：伪造客户端对象，只验证页面行为与请求参数。
"""

import os
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

VERSION = {"model_version_id": "ef31e86c-44a0-5925-bb71-26c41da39d32", "version": "credit-logreg-v1",
           "active": True, "model_id": "model", "status": "validated", "manifest_sha256": "hash"}
PREDICTION = "5f0f2f2e-1111-4222-8333-944455556666"
REPLAY = "7a1b2c3d-1111-4222-8333-944455556666"

EVIDENCE = {
    "prediction_id": PREDICTION, "principal_id": "actor", "model_version_id": VERSION["model_version_id"],
    "model_version": "credit-logreg-v1", "model_id": "model", "manifest_sha256": "hash",
    "status": "success", "risk_probability": 0.42, "error_code": None, "inference_ms": 3.5,
    "inference_attempted": True, "request_received_at": "2026-11-12T10:00:05Z",
    "completed_at": "2026-11-12T10:00:05Z", "http_status": 201, "explanation": {"top_factors": []},
    "missing_fields": [], "input_sha256": "c" * 64, "normalized_input": {"age": 30},
    "original_input": {"age": 30}, "decision_threshold": 0.5, "decision": "below_threshold",
    "window_id": "window", "alerts": [], "alert_actions": [],
}

TASK = {"replay_id": REPLAY, "prediction_id": PREDICTION, "model_version_id": VERSION["model_version_id"],
        "environment_check": "verified", "original_probability": 0.42, "replay_probability": 0.42,
        "absolute_error": 0.0, "decision_match": True, "status": "passed", "failure_reason": None,
        "requested_by": "actor", "created_at": "2026-11-12T10:01:00Z",
        "finished_at": "2026-11-12T10:01:02Z"}


class FakeClient:
    def __init__(self):
        self.calls = []
        self.records = {"items": [{"prediction_id": PREDICTION, "model_version_id": VERSION["model_version_id"],
                                   "status": "success", "risk_probability": 0.42,
                                   "request_received_at": "2026-11-12T10:00:05Z", "inference_ms": 3.5}],
                        "offset": 0, "limit": 30}
        self.evidence = dict(EVIDENCE)
        self.task = dict(TASK)

    def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        if path.endswith("/model-versions"):
            return {"items": [VERSION]}
        if path.endswith("/audit/records") and method == "GET":
            return self.records
        if path.startswith("/api/v1/predictions/"):
            return self.evidence
        if method == "POST" and path.endswith("/replay-runs"):
            return self.task
        if "/replay-runs/" in path:
            return self.task
        raise AssertionError(f"unexpected request {method} {path}")

    def download(self, path, *, params=None):
        self.calls.append(("DOWNLOAD", path, {"params": params}))
        return b'{"prediction_id": "x"}', {"x-file-sha256": "a" * 64}


class PageTestBase(unittest.TestCase):
    """补丁在测试期间一直生效：点击后的页面重跑同样需要伪造客户端。"""

    def setUp(self):
        self.client = FakeClient()
        for target in ("ui.pages.audit.client", "ui.pages.replay.client", "ui.common.client"):
            patcher = patch(target, return_value=self.client)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_page(self, name, roles=("auditor",)):
        script = f"from ui.pages.{name} import render" + chr(10) + "render()"
        app = AppTest.from_string(script, default_timeout=10)
        app.session_state["identity"] = {"principal_id": "actor", "roles": list(roles), "model_ids": ["model"]}
        app.session_state["token"] = "not-a-real-token"
        app.run()
        assert not app.exception, app.exception
        return app


class AuditPageTests(PageTestBase):
    def test_non_auditor_sees_guidance_and_no_request(self):
        app = self.run_page("audit", roles=("scorer",))
        self.assertTrue(any("audit" in info.value for info in app.info))
        self.assertFalse(any(path.endswith("/audit/records") for _, path, _ in self.client.calls))

    def test_records_and_evidence_are_rendered(self):
        app = self.run_page("audit")
        self.assertEqual(1, len(app.dataframe))
        self.assertTrue(any(metric.label == "风险概率" for metric in app.metric))
        audit_call = next(c for c in self.client.calls if c[1].endswith("/audit/records"))
        self.assertIn("from", audit_call[2]["params"])
        self.assertIn("to", audit_call[2]["params"])

    def test_withheld_raw_input_is_explained(self):
        self.client.evidence = {**EVIDENCE, "original_input": None, "raw_input_withheld": True}
        app = self.run_page("audit", roles=("admin",))
        self.assertTrue(any("原始输入仅对审计人员" in info.value for info in app.info))

    def test_export_prepares_download_with_digest(self):
        app = self.run_page("audit")
        prepare = next(button for button in app.button if "JSON" in button.label)
        prepare.click().run()
        self.assertTrue(any(call[0] == "DOWNLOAD" for call in self.client.calls))
        self.assertEqual(1, len(app.download_button))
        self.assertIn("SHA-256", app.download_button[0].label)


class ReplayPageTests(PageTestBase):
    def test_non_auditor_sees_guidance(self):
        app = self.run_page("replay", roles=("scorer",))
        self.assertTrue(any("audit" in info.value for info in app.info))

    def test_create_sends_idempotent_post(self):
        app = self.run_page("replay")
        app.text_input[0].input(PREDICTION)
        next(button for button in app.button if button.label == "发起复现").click().run()
        posts = [call for call in self.client.calls if call[0] == "POST"]
        self.assertEqual(1, len(posts))
        self.assertEqual(PREDICTION, posts[0][2]["body"]["prediction_id"])
        self.assertTrue(posts[0][2]["key"], "replay creation must use an idempotency key")

    def test_passed_task_shows_comparison(self):
        app = self.run_page("replay")
        app.text_input[1].input(REPLAY)
        app.run()
        labels = [metric.label for metric in app.metric]
        self.assertIn("原概率", labels)
        self.assertIn("复现概率", labels)
        self.assertTrue(any("误差在 1e-8 以内" in success.value for success in app.success))

    def test_failed_task_explains_reason(self):
        self.client.task = {**TASK, "status": "failed", "environment_check": "environment_mismatch",
                            "failure_reason": "environment mismatch", "replay_probability": None}
        app = self.run_page("replay")
        app.text_input[1].input(REPLAY)
        app.run()
        self.assertTrue(any("环境不符" in caption.value for caption in app.caption))
        self.assertTrue(any("environment mismatch" in error.value for error in app.error))


if __name__ == "__main__":
    unittest.main()
