"""A4 fixed-clock, real-SQLite reliability and monitoring acceptance tests."""

from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.config import Settings
from app.contracts import Principal
from app.demo_auth import DemoTokenAuthenticator
from app.main import create_app
from app.modules.alerts.commands import AlertCommands
from app.modules.models.bundle import DEFAULT_ARTIFACT_ROOT, load_registered
from app.modules.models.schema import normalize_input
from app.modules.monitoring.schema import (alerts, alert_actions, alert_revisions, metrics, metric_revisions,
                                            processed, rules, windows)
from app.modules.monitoring.statistics import calculate, psi
from app.modules.monitoring.store import LeaseLost, MonitoringStore
from app.modules.monitoring.time_utils import stamp, window_bounds
from app.modules.scoring.service import LocalModelPredictor, PREDICTION_ROUTE, json_digest
from app.storage.sqlite_store import SqlitePredictionStore, outbox
from app.worker import MonitoringWorker

BASE = datetime(2026, 11, 5, tzinfo=timezone.utc)
ADMIN = "a4-test-only-admin-" + "a" * 40
SCORER = "a4-test-only-scorer-" + "b" * 40


class Fixture:
    def setUp(self):
        self.parent = Path(__file__).resolve().parent
        self.temp = tempfile.TemporaryDirectory(dir=self.parent, prefix=".a4-test-")
        self.root = Path(self.temp.name)
        self.addCleanup(self.cleanup_temp)
        self.settings = Settings(database_url="sqlite:///" + (self.root / "monitor.db").as_posix(), admin_token=ADMIN, scorer_token=SCORER)
        self.base = SqlitePredictionStore(self.settings.database_url)
        self.addCleanup(self.base.close)
        descriptors = LocalModelPredictor(DEFAULT_ARTIFACT_ROOT).descriptors()
        self.base.initialize(descriptors)
        self.descriptors = {r["version"]: r for r in descriptors}
        self.v1 = self.descriptors["credit-logreg-v1"]
        self.monitor = MonitoringStore(self.base)
        self.monitor.initialize()
        self.now = BASE + timedelta(seconds=15)
        self.worker = MonitoringWorker(self.monitor, DEFAULT_ARTIFACT_ROOT, clock=lambda: self.now)
        self.actor = DemoTokenAuthenticator(ADMIN, SCORER).authenticate(ADMIN)
        self.sequence = 0

    def cleanup_temp(self):
        if not self.root.resolve().is_relative_to(self.parent):
            raise RuntimeError("cleanup escaped tests workspace")
        self.temp.cleanup()

    def enqueue(self, *, missing=False, status="success", attempted=True, latency=5.0, received=None, descriptor=None):
        self.sequence += 1
        version = descriptor or self.v1
        raw = {"customer_ref": f"SYN-A4-UNIT-{self.sequence}", "age": 35,
               "monthly_income": None if missing else "8000.00", "debt_income_ratio": 0.4,
               "past_due_count": 1, "loan_amount": "100000.00", "employment_years": 10}
        normalized = normalize_input(raw)
        received = received or BASE + timedelta(seconds=2)
        body = self.base.save_prediction({"principal_id": self.actor.principal_id, "route": PREDICTION_ROUTE,
                "key": f"fixture-{self.sequence}", "digest": json_digest(raw), "version_id": version["model_version_id"],
                "version": version["version"], "manifest_sha256": version["manifest_sha256"], "original_input": raw,
                "normalized_input": normalized, "missing_fields": [k for k, v in normalized.items() if v is None],
                "input_sha256": json_digest(raw), "received_at": stamp(received),
                "completed_at": stamp(received + timedelta(milliseconds=5)),
                "outcome": {"status": status, "risk_probability": 0.2 if status == "success" else None,
                            "decision_threshold": 0.5 if status == "success" else None,
                            "decision": "below_threshold" if status == "success" else None,
                            "explanation": {"unit_fixture": True} if status == "success" else None,
                            "inference_attempted": attempted, "inference_ms": latency if attempted else None,
                            "error_code": None if status == "success" else "INFERENCE_FAILED" if attempted else "MODEL_PACKAGE_UNAVAILABLE"}}).body
        return body["event_id"]

    def count(self, table):
        with self.base.engine.connect() as conn:
            return conn.execute(select(func.count()).select_from(table)).scalar_one()

    def metric(self, name, feature=""):
        with self.base.engine.connect() as conn:
            return dict(conn.execute(select(metrics).where(metrics.c.metric_name == name, metrics.c.feature == feature)).mappings().one())

    def finish(self):
        self.now = BASE + timedelta(seconds=70)
        return self.worker.run_once(max_events=1000)


class StatisticTests(unittest.TestCase):
    def setUp(self):
        self.reference = load_registered("credit-logreg-v1").reference_distribution

    def record(self, latency=5, status="success", attempted=True):
        return {"status": status, "risk_probability": 0.2 if status == "success" else None,
                "inference_attempted": attempted, "inference_ms": latency if attempted else None,
                "normalized_input": {f: None for f in self.reference["features"]}}

    def test_psi_matches_independent_hand_formula_and_cutpoint_boundary(self):
        reference = {"cutpoints": [0.5], "proportions_including_missing": [0.5, 0.5, 0]}
        value, bins = psi([0, 0, 0.5, 1], reference, 1e-6)
        self.assertEqual(bins, [2, 2, 0])
        self.assertAlmostEqual(value, 0)
        value, bins = psi([0, 0, 0, 1], reference, 1e-6)
        p, q = [0.5, 0.5, 0], [0.75, 0.25, 0]
        p, q = [(v + 1e-6) / (1 + 3e-6) for v in p], [(v + 1e-6) / (1 + 3e-6) for v in q]
        expected = sum((right - left) * math.log(right / left) for left, right in zip(p, q))
        self.assertAlmostEqual(value, expected, places=12)

    def test_empty_statistics_never_fabricate_zero_values(self):
        rows = calculate([], self.reference)
        self.assertEqual(len(rows), 15)
        self.assertTrue(all(r["status"] == "no_data" and r["value"] is None for r in rows))

    def test_failure_rate_excludes_load_failures(self):
        records = [self.record(), self.record(status="failed"), self.record(status="failed", attempted=False)]
        by_name = {(r["metric_name"], r["feature"]): r for r in calculate(records, self.reference)}
        self.assertEqual(by_name[("failure_rate", "")]["value"], 0.5)
        self.assertEqual(by_name[("failure_rate", "")]["sample_count"], 2)
        self.assertEqual(by_name[("missing_rate", "monthly_income")]["value"], 1)

    def test_p95_uses_only_successes_and_linear_interpolation(self):
        records = [self.record(latency=i) for i in range(1, 21)] + [self.record(latency=10000, status="failed")]
        row = next(r for r in calculate(records, self.reference) if r["metric_name"] == "inference_p95")
        self.assertAlmostEqual(row["value"], 19.05)

    def test_all_missing_psi_stays_finite_with_zero_reference_bin(self):
        rows = calculate([self.record() for _ in range(200)], self.reference)
        self.assertTrue(all(math.isfinite(r["value"]) for r in rows if r["status"] == "ready"))

    def test_window_uses_utc_minute_and_exclusive_end(self):
        start, end = window_bounds("2026-11-05T08:00:59.999999+08:00")
        self.assertEqual(start, "2026-11-05T00:00:00.000000Z")
        self.assertEqual(window_bounds(end)[0], end)


class MonitoringWorkerTests(Fixture, unittest.TestCase):
    def test_consumption_is_durable_before_window_closes(self):
        self.enqueue()
        result = self.worker.run_once()
        self.assertEqual(result["consumed"], 1)
        self.assertEqual(self.count(processed), 1)
        self.assertEqual(self.count(metrics), 0)
        with self.base.engine.connect() as conn:
            self.assertEqual(conn.execute(select(outbox.c.status)).scalar_one(), "done")

    def test_finalization_waits_for_end_plus_ten_second_grace(self):
        self.enqueue()
        self.now = BASE + timedelta(seconds=69, milliseconds=999)
        self.assertEqual(self.worker.run_once()["windows_computed"], 0)
        self.now = BASE + timedelta(seconds=70)
        self.assertEqual(self.worker.run_once()["windows_computed"], 1)
        self.assertEqual(self.count(metrics), 15)

    def test_replayed_outbox_does_not_duplicate_counts_or_metrics(self):
        event_id = self.enqueue(missing=True)
        self.finish()
        with self.base._write() as conn:
            conn.execute(outbox.update().where(outbox.c.event_id == event_id).values(status="pending"))
        result = self.worker.run_once()
        self.assertEqual(result["duplicates"], 1)
        self.assertEqual(self.count(processed), 1)
        self.assertEqual(self.count(metric_revisions), 15)
        self.assertEqual(self.count(alerts), 1)

    def test_expired_lease_is_reclaimed_and_stale_owner_is_fenced(self):
        self.enqueue()
        old = self.monitor.claim(self.now)
        self.assertIsNone(self.monitor.claim(self.now))
        self.now += timedelta(seconds=31)
        new = self.monitor.claim(self.now)
        self.assertNotEqual(old["lease_token"], new["lease_token"])
        with self.assertRaises(LeaseLost):
            self.monitor.consume(old, self.now)
        self.monitor.fail_event(old, self.now, "OLD_OWNER")
        self.assertTrue(self.monitor.consume(new, self.now))
        self.assertEqual(self.count(processed), 1)

    def test_five_crashed_claims_go_to_dead_letter(self):
        self.enqueue()
        for _ in range(5):
            self.assertIsNotNone(self.monitor.claim(self.now))
            self.now += timedelta(seconds=31)
        self.assertIsNone(self.monitor.claim(self.now))
        self.assertEqual(self.monitor.status(self.now)["queue"]["dead_letter"], 1)
        self.assertEqual(self.count(processed), 0)

    def test_parallel_claims_cannot_own_the_same_live_lease(self):
        self.enqueue()
        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(lambda _: self.monitor.claim(self.now), range(2)))
        self.assertEqual(sum(claim is not None for claim in claims), 1)

    def test_prediction_worker_does_not_reclaim_other_task_types(self):
        event_id = self.enqueue()
        with self.base._write() as conn:
            conn.execute(outbox.update().where(outbox.c.event_id == event_id).values(event_type="future.other_task",
                         status="processing", attempts=5, lease_expires_at=stamp(BASE)))
        self.assertIsNone(self.monitor.claim(self.now))
        with self.base.engine.connect() as conn:
            self.assertEqual(conn.execute(select(outbox.c.status)).scalar_one(), "processing")

    def test_modified_input_evidence_is_not_consumed(self):
        self.enqueue()
        from app.storage.sqlite_store import snapshots
        with self.base._write() as conn:
            original = dict(conn.execute(select(snapshots.c.original_input)).scalar_one())
            original["age"] = 36
            conn.execute(snapshots.update().values(original_input=original))
        self.assertEqual(self.worker.run_once()["event_errors"], 1)
        self.assertEqual(self.count(processed), 0)

    def test_poison_retries_are_bounded_and_do_not_block_next_event(self):
        bad = self.enqueue()
        self.enqueue()
        with self.base._write() as conn:
            conn.execute(outbox.update().where(outbox.c.event_id == bad).values(schema_version="unsupported"))
        first = self.worker.run_once()
        self.assertEqual((first["event_errors"], first["consumed"]), (1, 1))
        for seconds in (1, 2, 4, 8):
            self.now += timedelta(seconds=seconds)
            self.worker.run_once()
        self.assertEqual(self.monitor.status(self.now)["queue"]["dead_letter"], 1)
        self.assertEqual(self.count(processed), 1)

    def test_consumer_mark_window_and_done_roll_back_together(self):
        self.enqueue()
        def fail(stage):
            if stage == "window":
                raise RuntimeError("test fault")
        with patch.object(self.monitor, "after_write", side_effect=fail):
            self.assertEqual(self.worker.run_once()["event_errors"], 1)
        self.assertEqual(self.count(processed), 0)
        self.assertEqual(self.count(windows), 0)
        self.now += timedelta(seconds=1)
        self.assertEqual(self.worker.run_once()["consumed"], 1)

    def test_metric_alert_and_computed_revision_roll_back_together(self):
        self.enqueue(missing=True)
        def fail(stage):
            if stage == "alerts":
                raise RuntimeError("test fault")
        with patch.object(self.monitor, "after_write", side_effect=fail):
            self.assertEqual(self.finish()["window_errors"], 1)
        self.assertEqual(self.count(metrics), 0)
        self.assertEqual(self.count(alerts), 0)
        self.assertEqual(self.count(processed), 1)
        self.now += timedelta(seconds=1)
        self.assertEqual(self.worker.run_once()["windows_computed"], 1)
        self.assertEqual(self.count(metric_revisions), 15)

    def test_restart_finishes_dirty_window_without_unconsumed_events(self):
        self.enqueue()
        claimed = self.monitor.claim(self.now)
        self.monitor.consume(claimed, self.now)
        restarted = MonitoringWorker(self.monitor, DEFAULT_ARTIFACT_ROOT, clock=lambda: BASE + timedelta(seconds=70))
        result = restarted.run_once()
        self.assertEqual((result["consumed"], result["windows_computed"]), (0, 1))

    def test_late_events_correct_original_window_without_deleting_alert(self):
        self.enqueue(missing=True)
        self.finish()
        with self.base.engine.connect() as conn:
            alert_id = conn.execute(select(alerts.c.alert_id)).scalar_one()
        for _ in range(20):
            self.enqueue()
        self.now += timedelta(seconds=10)
        self.worker.run_once()
        self.assertAlmostEqual(self.metric("missing_rate", "monthly_income")["value"], 1 / 21)
        with self.base.engine.connect() as conn:
            row = conn.execute(select(alerts)).mappings().one()
            self.assertEqual(row["alert_id"], alert_id)
            self.assertFalse(row["condition_active"])
            self.assertEqual(row["status"], "open")
        self.assertEqual(self.count(alert_revisions), 2)
        self.assertEqual(self.count(metric_revisions), 30)

    def test_late_correction_never_reopens_closed_alert(self):
        self.enqueue(missing=True)
        self.finish()
        with self.base.engine.connect() as conn:
            alert_id = conn.execute(select(alerts.c.alert_id)).scalar_one()
        commands = AlertCommands(self.monitor)
        commands.act(self.actor, alert_id, "acknowledge", "reviewing", "ack", self.now)
        commands.act(self.actor, alert_id, "close", "synthetic scenario confirmed", "close", self.now)
        self.enqueue(missing=True)
        self.worker.run_once()
        with self.base.engine.connect() as conn:
            self.assertEqual(conn.execute(select(alerts.c.status)).scalar_one(), "closed")
        self.assertEqual(self.count(alerts), 1)
        self.assertEqual(self.count(alert_revisions), 2)

    def test_rules_are_frozen_per_window_and_new_rules_affect_future_windows(self):
        self.enqueue(missing=True)
        self.worker.run_once()
        body = {"model_version_id": self.v1["model_version_id"], "metric_name": "missing_rate", "feature": "monthly_income",
                "threshold": 0.9, "min_samples": 1, "effective_at": stamp(BASE + timedelta(minutes=1))}
        created = AlertCommands(self.monitor).create_rule(self.actor, body, "new-rule", self.now)
        self.enqueue(missing=True, received=BASE + timedelta(seconds=61))
        self.enqueue(received=BASE + timedelta(seconds=62))
        self.now = BASE + timedelta(seconds=130)
        self.worker.run_once()
        with self.base.engine.connect() as conn:
            snapshots = conn.execute(select(windows.c.rule_snapshot).order_by(windows.c.window_start)).scalars().all()
        selected = [[r for r in group if r["metric_name"] == "missing_rate" and r["feature"] == "monthly_income"][0] for group in snapshots]
        self.assertEqual([r["threshold"] for r in selected], [0.05, 0.9])
        self.assertEqual(selected[1]["rule_id"], created.body["rule_id"])

    def test_psi_requires_200_successful_samples(self):
        for _ in range(199):
            self.enqueue()
        self.finish()
        self.assertEqual(self.metric("feature_psi", "age")["status"], "insufficient_samples")
        self.assertEqual(self.count(alerts), 0)
        self.enqueue()
        self.worker.run_once()
        self.assertEqual(self.metric("feature_psi", "age")["status"], "ready")
        self.assertTrue(math.isfinite(self.metric("feature_psi", "age")["value"]))
        self.assertGreater(self.count(alerts), 0)

    def test_windows_isolate_model_versions(self):
        self.enqueue()
        self.enqueue(descriptor=self.descriptors["credit-logreg-v2"])
        self.finish()
        self.assertEqual(self.count(windows), 2)
        self.assertEqual(self.count(metrics), 30)
        with self.base.engine.connect() as conn:
            self.assertEqual(len(set(conn.execute(select(metrics.c.reference_manifest_sha256)).scalars())), 2)

    def test_reference_failure_leaves_dirty_window_and_recovers(self):
        self.enqueue()
        self.worker.artifact_root = self.root / "missing-artifacts"
        self.assertEqual(self.finish()["window_errors"], 1)
        self.assertEqual(self.count(metrics), 0)
        self.worker.artifact_root = DEFAULT_ARTIFACT_ROOT
        self.now += timedelta(seconds=1)
        self.assertEqual(self.worker.run_once()["windows_computed"], 1)

    def test_five_calculation_failures_are_visible_for_manual_inspection(self):
        self.enqueue()
        self.worker.artifact_root = self.root / "missing-artifacts"
        self.finish()
        for seconds in (1, 2, 4, 8):
            self.now += timedelta(seconds=seconds)
            self.worker.run_once()
        self.assertEqual(self.monitor.status(self.now)["failed_windows"], 1)
        self.assertEqual(self.worker.run_once()["window_errors"], 0)

    def test_manual_retry_recovers_quarantined_window_without_reconsuming(self):
        self.enqueue()
        self.worker.artifact_root = self.root / "missing-artifacts"
        self.finish()
        for seconds in (1, 2, 4, 8):
            self.now += timedelta(seconds=seconds)
            self.worker.run_once()
        with self.base.engine.connect() as conn:
            window_id = conn.execute(select(windows.c.window_id)).scalar_one()
        self.monitor.retry_failed("window", window_id, "Restored approved model package", self.now)
        self.worker.artifact_root = DEFAULT_ARTIFACT_ROOT
        self.assertEqual(self.worker.run_once()["windows_computed"], 1)
        self.assertEqual(self.count(processed), 1)

    def test_revision_guard_rejects_a_stale_calculation(self):
        self.enqueue()
        def arrive(_window):
            self.enqueue()
            self.monitor.consume(self.monitor.claim(self.now), self.now)
        with patch.object(self.worker, "before_publish", side_effect=arrive):
            result = self.finish()
        self.assertEqual(result["stale_calculations"], 1)
        self.assertEqual(self.count(metrics), 0)
        self.worker.run_once()
        self.assertEqual(self.metric("failure_rate")["sample_count"], 2)

    def test_additive_migration_preserves_a3_event_data(self):
        self.enqueue()
        with self.base._write() as conn:
            conn.exec_driver_sql("ALTER TABLE outbox_events DROP COLUMN lease_token")
        self.monitor.initialize()
        self.assertEqual(self.worker.run_once()["consumed"], 1)
        self.assertEqual(self.count(processed), 1)

    def test_heartbeat_exposes_never_started_fresh_and_stale(self):
        self.assertEqual(self.monitor.status(self.now)["worker"]["state"], "never_started")
        self.worker.run_once()
        self.assertEqual(self.monitor.status(self.now)["worker"]["state"], "fresh")
        self.now += timedelta(seconds=16)
        self.assertEqual(self.monitor.status(self.now)["worker"]["state"], "stale")


class MonitoringApiTests(Fixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.app = create_app(self.settings, store=self.base)
        self.client = self.enterContext(TestClient(self.app))
        self.headers = {"Authorization": "Bearer " + ADMIN, "Idempotency-Key": "monitor-command"}

    def test_read_metrics_and_dispose_alert_with_idempotent_history(self):
        self.enqueue(missing=True)
        self.finish()
        params = {"model_version_id": self.v1["model_version_id"], "limit": 100}
        response = self.client.get("/api/v1/metrics", params=params, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["items"]), 15)
        self.assertTrue(all(not r["stale"] for r in response.json()["items"]))
        alert_id = self.client.get("/api/v1/alerts", params=params, headers=self.headers).json()["items"][0]["alert_id"]
        endpoint = f"/api/v1/alerts/{alert_id}"
        self.assertEqual(self.client.post(endpoint + "/close", headers=self.headers, json={"note": "done"}).status_code, 409)
        self.assertEqual(self.client.post(endpoint + "/acknowledge", headers=self.headers, json={"note": "checking"}).status_code, 200)
        close_headers = {**self.headers, "Idempotency-Key": "close-alert"}
        self.assertEqual(self.client.post(endpoint + "/close", headers=close_headers, json={}).status_code, 422)
        first = self.client.post(endpoint + "/close", headers=close_headers, json={"note": "confirmed synthetic input"})
        second = self.client.post(endpoint + "/close", headers=close_headers, json={"note": "confirmed synthetic input"})
        self.assertEqual(first.json(), second.json())
        self.assertEqual(second.headers["Idempotency-Replayed"], "true")
        detail = self.client.get(endpoint, headers=self.headers).json()
        self.assertEqual((detail["status"], len(detail["actions"]), len(detail["revisions"])), ("closed", 2, 1))

    def test_stale_metrics_are_explicit_while_late_revision_is_pending(self):
        self.enqueue()
        self.finish()
        self.enqueue()
        self.monitor.consume(self.monitor.claim(self.now), self.now)
        rows = self.client.get("/api/v1/metrics", params={"model_version_id": self.v1["model_version_id"]}, headers=self.headers).json()["items"]
        self.assertTrue(all(r["stale"] for r in rows))

    def test_metrics_expose_known_unconsumed_backlog_before_revision_changes(self):
        self.enqueue()
        self.finish()
        self.enqueue()
        rows = self.client.get("/api/v1/metrics", params={"model_version_id": self.v1["model_version_id"]}, headers=self.headers).json()["items"]
        self.assertTrue(all(r["stale"] and r["unconsumed_events"] == 1 for r in rows))

    def test_rule_creation_is_versioned_idempotent_and_cannot_backdate(self):
        body = {"model_version_id": self.v1["model_version_id"], "metric_name": "missing_rate", "feature": "monthly_income",
                "threshold": 0.2, "min_samples": 1, "effective_at": stamp(BASE + timedelta(minutes=1))}
        with patch("app.modules.monitoring.routes.now_utc", return_value=BASE):
            first = self.client.post("/api/v1/monitor-rules", headers=self.headers, json=body)
            self.assertEqual(first.status_code, 200, first.text)
            self.assertEqual(first.json()["rule_version"], 2)
            conflict = self.client.post("/api/v1/monitor-rules", headers=self.headers, json={**body, "threshold": 0.3})
            self.assertEqual(conflict.status_code, 409)
        with patch("app.modules.monitoring.routes.now_utc", return_value=BASE + timedelta(minutes=10)):
            self.assertEqual(self.client.post("/api/v1/monitor-rules", headers=self.headers, json=body).json(), first.json())
            self.assertEqual(self.client.post("/api/v1/monitor-rules", headers={**self.headers, "Idempotency-Key": "backdate"}, json=body).status_code, 422)

    def test_invalid_policy_threshold_feature_and_sample_count_are_rejected(self):
        body = {"model_version_id": self.v1["model_version_id"], "metric_name": "missing_rate", "feature": "monthly_income",
                "threshold": 0.2, "min_samples": 1, "effective_at": stamp(BASE + timedelta(minutes=1))}
        with patch("app.modules.monitoring.routes.now_utc", return_value=BASE):
            for change in ({"threshold": 2}, {"feature": "unknown"}, {"metric_name": "feature_psi", "min_samples": 199},
                           {"effective_at": "2026-11-05T00:01:00"}):
                with self.subTest(change=change):
                    self.assertEqual(self.client.post("/api/v1/monitor-rules", headers=self.headers, json={**body, **change}).status_code, 422)

    def test_permissions_and_pagination_are_enforced(self):
        params = {"model_version_id": self.v1["model_version_id"]}
        self.assertEqual(self.client.get("/api/v1/metrics", params=params).status_code, 401)
        self.assertEqual(self.client.get("/api/v1/metrics", params={**params, "limit": 101}, headers=self.headers).status_code, 422)
        scorer = {"Authorization": "Bearer " + SCORER, "Idempotency-Key": "not-admin"}
        self.assertEqual(self.client.get("/api/v1/monitor/status", headers=scorer).status_code, 403)
        self.assertEqual(self.client.get("/api/v1/monitor/status", headers=self.headers).status_code, 200)
        with patch.object(DemoTokenAuthenticator, "authenticate", return_value=Principal("no-scope", frozenset({"scorer"}), frozenset())):
            self.assertEqual(self.client.get("/api/v1/metrics", params=params, headers=scorer).status_code, 403)
