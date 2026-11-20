"""A3 HTTP + file-backed SQLite integration and fault-injection acceptance."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.config import Settings
from app.contracts import DomainError, Principal, StorageUnavailable
from app.demo_auth import DemoTokenAuthenticator
from app.main import create_app
from app.modules.models.bundle import DEFAULT_ARTIFACT_ROOT
from app.modules.scoring.service import PredictionFailure
from app.storage.sqlite_store import (activation_receipts, audit_logs, metadata, outbox,
                                      predictions, snapshots)

TEST_ADMIN = "a3-integration-test-admin-" + "a" * 40
TEST_SCORER = "a3-integration-test-scorer-" + "b" * 40


class ScoringApiTests(unittest.TestCase):
    def setUp(self):
        self.parent = Path(__file__).resolve().parent
        self.temp = tempfile.TemporaryDirectory(dir=self.parent, prefix=".a3-test-")
        self.root = Path(self.temp.name)
        self.addCleanup(self.cleanup_temp)
        self.settings = Settings(database_url="sqlite:///" + (self.root / "platform.db").as_posix(),
                                 admin_token=TEST_ADMIN, scorer_token=TEST_SCORER)
        self.app = create_app(self.settings)
        self.client = self.enterContext(TestClient(self.app))
        self.store = self.app.state.store
        self.predictor = self.app.state.predictor
        self.raw = json.loads((self.parent.parent / "examples/A2-score-input.json").read_text(encoding="utf-8"))
        items = self.client.get("/api/v1/model-versions", headers=self.headers()).json()["items"]
        self.ids = {v["version"]: v["model_version_id"] for v in items}
        self.v1, self.v2 = self.ids["credit-logreg-v1"], self.ids["credit-logreg-v2"]
        self.assertEqual(self.activate(self.v1).status_code, 200)

    def cleanup_temp(self):
        if not self.root.resolve().is_relative_to(self.parent):
            raise RuntimeError("cleanup target escaped tests directory")
        self.temp.cleanup()

    @staticmethod
    def headers(key="test-key", token=TEST_ADMIN):
        return {"Authorization": "Bearer " + token, "Idempotency-Key": key}

    def activate(self, version_id, key="activate-v1", **kwargs):
        return self.client.post(f"/api/v1/model-versions/{version_id}/activate", headers=self.headers(key), **kwargs)

    def score(self, key="test-key", raw=None, version=None, token=TEST_SCORER):
        return self.client.post("/api/v1/predictions", headers=self.headers(key, token),
                                json={"model_version_id": version or self.v1, "input": self.raw if raw is None else raw})

    def counts(self):
        with self.store.engine.connect() as conn:
            return {table.name: conn.execute(select(func.count()).select_from(table)).scalar_one()
                    for table in (snapshots, predictions, audit_logs, outbox, activation_receipts)}

    def test_success_commits_score_snapshot_audit_and_outbox(self):
        response = self.score()
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["status"], "success")
        self.assertAlmostEqual(body["risk_probability"], 0.3088120976806297, places=12)
        self.assertTrue(body["inference_attempted"])
        self.assertGreaterEqual(body["inference_ms"], 0)
        self.assertEqual(self.counts(), {"input_snapshots": 1, "predictions": 1, "audit_logs": 2,
                                        "outbox_events": 1, "activation_receipts": 1})
        with self.store.engine.connect() as conn:
            snapshot = conn.execute(select(snapshots)).mappings().one()
            event = conn.execute(select(outbox)).mappings().one()
            self.assertEqual(snapshot["monthly_income_cents"], 800000)
            self.assertEqual(snapshot["loan_amount_cents"], 10000000)
            self.assertEqual(snapshot["original_input"], self.raw)
            self.assertEqual(event["status"], "pending")
            self.assertEqual(event["payload"]["prediction_id"], body["prediction_id"])
            self.assertNotIn("original_input", event["payload"])
            self.assertEqual(event["schema_version"], "1.1")

    def test_ten_retries_return_one_record_and_skip_sequential_inference(self):
        with patch.object(self.predictor, "score", wraps=self.predictor.score) as predictor:
            replies = [self.score() for _ in range(10)]
            self.assertEqual(predictor.call_count, 1)
        self.assertTrue(all(r.status_code == 201 for r in replies))
        self.assertTrue(all(r.json() == replies[0].json() for r in replies))
        self.assertEqual(replies[0].headers["Idempotency-Replayed"], "false")
        self.assertEqual(replies[-1].headers["Idempotency-Replayed"], "true")
        self.assertEqual(self.counts()["outbox_events"], 1)

    def test_same_key_changed_input_returns_conflict(self):
        self.score()
        response = self.score(raw={**self.raw, "debt_income_ratio": 0.8})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "IDEMPOTENCY_CONFLICT")
        self.assertEqual(self.counts()["predictions"], 1)

    def test_key_order_does_not_change_request_digest(self):
        first = self.score()
        second = self.score(raw=dict(reversed(list(self.raw.items()))))
        self.assertEqual(first.json(), second.json())

    def test_idempotency_is_scoped_to_authenticated_principal(self):
        first = self.score(token=TEST_SCORER)
        second = self.score(token=TEST_ADMIN)
        self.assertNotEqual(first.json()["prediction_id"], second.json()["prediction_id"])
        self.assertEqual(self.counts()["predictions"], 2)

    def test_same_key_different_version_conflicts(self):
        self.score()
        self.activate(self.v2, "activate-v2")
        self.assertEqual(self.score(version=self.v2).status_code, 409)

    def test_inference_failure_is_committed_as_failed_with_null_probability(self):
        failure = PredictionFailure("INFERENCE_FAILED", {"inference_attempted": True, "inference_ms": 12.5})
        with patch.object(self.predictor, "score", side_effect=failure):
            response = self.score()
        self.assertEqual(response.status_code, 503)
        body = response.json()
        self.assertEqual(body["status"], "failed")
        self.assertIsNone(body["risk_probability"])
        self.assertIsNone(body["decision"])
        self.assertEqual(body["inference_ms"], 12.5)
        self.assertTrue(body["inference_attempted"])
        self.assertEqual(self.counts()["predictions"], 1)
        self.assertEqual(self.counts()["outbox_events"], 1)

    def test_failed_idempotent_replay_is_stable_even_after_model_recovery(self):
        with patch.object(self.predictor, "score", side_effect=PredictionFailure("MODEL_PACKAGE_UNAVAILABLE")):
            failed = self.score()
        replayed = self.score()
        self.assertEqual(replayed.status_code, 503)
        self.assertEqual(replayed.json(), failed.json())
        self.assertEqual(self.score(key="new-attempt").status_code, 201)

    def test_real_tampered_package_is_rejected_and_failure_recorded(self):
        copied = self.root / "copied-artifacts"
        shutil.copytree(DEFAULT_ARTIFACT_ROOT, copied)
        path = copied / "credit-logreg-v1" / "pipeline.joblib"
        path.write_bytes(path.read_bytes() + b"test-tamper")
        self.predictor.root = copied
        response = self.score()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error_code"], "MODEL_PACKAGE_UNAVAILABLE")
        self.assertFalse(response.json()["inference_attempted"])
        self.assertIsNone(response.json()["inference_ms"])

    def test_environment_mismatch_is_a_nonattempted_failure(self):
        with patch("app.modules.models.bundle.environment", return_value={}):
            response = self.score()
        self.assertEqual(response.json()["error_code"], "MODEL_ENVIRONMENT_MISMATCH")
        self.assertIsNone(response.json()["risk_probability"])
        self.assertFalse(response.json()["inference_attempted"])

    def test_soft_inference_deadline_records_actual_elapsed_time(self):
        self.predictor.inference_timeout_ms = 1e-12
        response = self.score()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error_code"], "INFERENCE_TIMEOUT")
        self.assertGreater(response.json()["inference_ms"], 0)

    def test_each_write_stage_failure_rolls_back_all_four_records(self):
        before = self.counts()
        for stage in ("snapshot", "prediction", "audit", "outbox"):
            with self.subTest(stage=stage):
                def fail(at):
                    if at == stage:
                        raise StorageUnavailable()
                with patch.object(self.store, "after_write", side_effect=fail):
                    response = self.score(key="rollback-" + stage)
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.json()["error"]["code"], "STORAGE_UNAVAILABLE")
                self.assertNotIn("prediction_id", response.json())
                self.assertEqual(self.counts(), before)
        self.assertEqual(self.score(key="rollback-outbox").status_code, 201)

    def test_activation_and_audit_roll_back_together(self):
        before = self.counts()
        def fail(stage):
            if stage == "activation_audit":
                raise StorageUnavailable()
        with patch.object(self.store, "after_write", side_effect=fail):
            response = self.activate(self.v2, "activate-v2")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.counts(), before)
        self.assertTrue(self.store.version(self.v1)["active"])
        self.assertFalse(self.store.version(self.v2)["active"])

    def test_activation_replay_does_not_duplicate_audit(self):
        before = self.counts()
        replay = self.activate(self.v1)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.headers["Idempotency-Replayed"], "true")
        self.assertEqual(self.counts(), before)

    def test_scorer_cannot_activate_model(self):
        response = self.client.post(f"/api/v1/model-versions/{self.v2}/activate", headers=self.headers(token=TEST_SCORER))
        self.assertEqual(response.status_code, 403)

    def test_missing_or_wrong_token_is_rejected(self):
        for headers in ({}, {"Authorization": "Bearer invalid", "Idempotency-Key": "test"}):
            response = self.client.get("/api/v1/model-versions", headers=headers)
            self.assertEqual(response.status_code, 401)

    def test_model_scope_is_checked_on_server(self):
        with patch.object(DemoTokenAuthenticator, "authenticate", return_value=Principal("out-of-scope", frozenset({"scorer"}), frozenset())):
            self.assertEqual(self.score().status_code, 403)
        self.assertEqual(self.counts()["predictions"], 0)

    def test_invalid_inputs_never_create_prediction_or_event(self):
        for change in ({"age": True}, {"age": 17}, {"loan_amount": "-1.00"},
                       {"monthly_income": "NaN"}, {"debt_income_ratio": 4},
                       {"monthly_income": "100.001"}, {"customer_ref": ["invalid"]}):
            with self.subTest(change=change):
                self.assertEqual(self.score(raw={**self.raw, **change}).status_code, 422)
        self.assertEqual(self.counts()["predictions"], 0)

    def test_labels_and_client_role_spoofing_are_rejected(self):
        self.assertEqual(self.score(raw={**self.raw, "synthetic_default_label": 1}).status_code, 422)
        response = self.client.post("/api/v1/predictions", headers=self.headers(),
                                    json={"model_version_id": self.v1, "input": self.raw, "role": "admin"})
        self.assertEqual(response.status_code, 422)

    def test_missing_invalid_or_oversized_key_is_rejected(self):
        for key in (None, "", "contains space", "x" * 129):
            headers = self.headers()
            if key is None:
                headers.pop("Idempotency-Key")
            else:
                headers["Idempotency-Key"] = key
            response = self.client.post("/api/v1/predictions", headers=headers,
                                        json={"model_version_id": self.v1, "input": self.raw})
            self.assertEqual(response.status_code, 422)

    def test_null_input_is_preserved_in_snapshot(self):
        response = self.score(raw={**self.raw, "monthly_income": None})
        self.assertEqual(response.status_code, 201)
        with self.store.engine.connect() as conn:
            snapshot = conn.execute(select(snapshots)).mappings().one()
            self.assertIsNone(snapshot["original_input"]["monthly_income"])
            self.assertIsNone(snapshot["monthly_income_cents"])
            self.assertIn("monthly_income", snapshot["missing_fields"])

    def test_cached_old_version_survives_activation_switch(self):
        old = self.score()
        self.activate(self.v2, "activate-v2")
        self.assertEqual(self.score().json(), old.json())
        self.assertEqual(self.score(key="new-old-version-request").status_code, 409)
        self.assertEqual(self.score(key="new-v2-request", version=self.v2).status_code, 201)

    def test_restart_preserves_pending_outbox_and_idempotency(self):
        original = self.score()
        with TestClient(create_app(self.settings)) as second:
            replay = second.post("/api/v1/predictions", headers=self.headers(token=TEST_SCORER),
                                  json={"model_version_id": self.v1, "input": self.raw})
            self.assertEqual(replay.json(), original.json())
            with second.app.state.store.engine.connect() as conn:
                self.assertEqual(conn.execute(select(outbox.c.status)).scalar_one(), "pending")

    def test_database_read_error_returns_no_false_score_success(self):
        with patch.object(self.store, "previous", side_effect=StorageUnavailable()):
            response = self.score()
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("prediction_id", response.json())
        self.assertEqual(self.counts()["predictions"], 0)

    def test_parallel_same_key_has_one_committed_record(self):
        with ThreadPoolExecutor(max_workers=5) as pool:
            replies = list(pool.map(lambda _: self.score(), range(5)))
        self.assertTrue(all(r.status_code == 201 for r in replies))
        self.assertTrue(all(r.json() == replies[0].json() for r in replies))
        self.assertEqual(self.counts()["predictions"], 1)
        self.assertEqual(self.counts()["outbox_events"], 1)

    def test_parallel_conflicting_payloads_have_one_winner(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            replies = list(pool.map(lambda debt: self.score(raw={**self.raw, "debt_income_ratio": debt}), (0.4, 0.8)))
        self.assertEqual(sorted(r.status_code for r in replies), [201, 409])
        self.assertEqual(self.counts()["predictions"], 1)

    def test_health_openapi_and_sqlite_settings(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok", "database": "reachable"})
        self.assertEqual(self.client.get("/openapi.json").status_code, 200)
        with self.store.engine.connect() as conn:
            self.assertEqual(conn.exec_driver_sql("PRAGMA foreign_keys").scalar(), 1)
            self.assertEqual(conn.exec_driver_sql("PRAGMA journal_mode").scalar(), "wal")

    def test_missing_or_duplicate_demo_secrets_fail_closed(self):
        for admin, scorer in (("", ""), (TEST_ADMIN, TEST_ADMIN)):
            with self.assertRaises(ValueError):
                create_app(replace(self.settings, admin_token=admin, scorer_token=scorer))


if __name__ == "__main__":
    unittest.main()
