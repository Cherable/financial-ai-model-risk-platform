"""A4 integration evidence: real scoring HTTP + independent worker invocations.

Finalization uses an explicitly advanced test clock to avoid waiting a minute.
This is not a latency or throughput benchmark.
"""

from contextlib import closing
from datetime import timedelta, timezone
import argparse
import json
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time

import httpx

from app.modules.monitoring.store import MonitoringStore
from app.modules.monitoring.time_utils import now_utc
from app.storage.sqlite_store import SqlitePredictionStore
from app.worker import MonitoringWorker

ROOT = Path(__file__).resolve().parents[1]


def verify():
    runtime = ROOT / "data" / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=runtime, prefix="a4-worker-") as temp:
        folder = Path(temp).resolve()
        if not folder.is_relative_to(runtime.resolve()):
            raise RuntimeError("temporary workspace escaped its root")
        database = folder / "platform.db"
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        admin, scorer = secrets.token_urlsafe(48), secrets.token_urlsafe(48)
        env = {**os.environ, "DATABASE_URL": "sqlite:///" + database.as_posix(),
               "ARTIFACT_ROOT": str(ROOT / "artifacts"), "APP_ADMIN_TOKEN": admin,
               "APP_SCORER_TOKEN": scorer, "INFERENCE_TIMEOUT_MS": "2000"}
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        server = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:create_app", "--factory",
                                   "--host", "127.0.0.1", "--port", str(port), "--log-level", "error"],
                                  env=env, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=flags)
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10, trust_env=False) as client:
                deadline = time.monotonic() + 25
                while True:
                    if server.poll() is not None:
                        raise RuntimeError("temporary API exited during startup")
                    try:
                        if client.get("/health").status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    if time.monotonic() >= deadline:
                        raise RuntimeError("temporary API did not start")
                    time.sleep(0.1)
                headers = {"Authorization": "Bearer " + admin, "Idempotency-Key": "a4-activate"}
                items = client.get("/api/v1/model-versions", headers=headers).json()["items"]
                version_id = next(r["model_version_id"] for r in items if r["version"] == "credit-logreg-v1")
                client.post(f"/api/v1/model-versions/{version_id}/activate", headers=headers).raise_for_status()
                sample = json.loads((ROOT / "examples/A2-score-input.json").read_text(encoding="utf-8"))
                for index in range(40):
                    features = {**sample, "customer_ref": f"SYN-A4-LIVE-{index:04d}"}
                    if index >= 30:
                        features["monthly_income"] = None
                    response = client.post("/api/v1/predictions", headers={"Authorization": "Bearer " + scorer,
                                           "Idempotency-Key": f"a4-live-{index}"},
                                           json={"model_version_id": version_id, "input": features})
                    response.raise_for_status()
                before = client.get("/api/v1/monitor/status", headers=headers).json()
                if before["queue"]["pending"] != 40:
                    raise RuntimeError("scoring must save while the worker is stopped")
                runs = []
                for _ in range(2):
                    run = subprocess.run([sys.executable, "-m", "app.worker", "--once", "--max-events", "100"],
                                         env=env, cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                                         creationflags=flags, timeout=30)
                    if run.returncode:
                        raise RuntimeError("independent worker process failed")
                    runs.append(json.loads(run.stdout.strip().splitlines()[-1]))
                if runs[0]["consumed"] != 40 or runs[1]["consumed"] != 0:
                    raise RuntimeError("worker process restart duplicated or lost events")
                # Test-only clock advance. Source request/score times are never rewritten.
                base = SqlitePredictionStore(env["DATABASE_URL"])
                try:
                    monitor = MonitoringStore(base)
                    monitor.initialize()
                    advanced = now_utc() + timedelta(seconds=120)
                    finalize = MonitoringWorker(monitor, ROOT / "artifacts", clock=lambda: advanced).run_once()
                finally:
                    base.close()
                params = {"model_version_id": version_id, "limit": 100}
                metric_rows = client.get("/api/v1/metrics", params=params, headers=headers).json()["items"]
                alert_rows = client.get("/api/v1/alerts", params=params, headers=headers).json()["items"]
                missing_alerts = [a for a in alert_rows if a["metric_name"] == "missing_rate" and a["feature"] == "monthly_income"]
                if not missing_alerts or not metric_rows or any(row["stale"] for row in metric_rows):
                    raise RuntimeError("finalized metrics and expected missing-income alerts are not visible")
                alert_id = missing_alerts[0]["alert_id"]
                client.post(f"/api/v1/alerts/{alert_id}/acknowledge", headers={**headers, "Idempotency-Key": "a4-ack"},
                            json={"note": "Reviewing synthetic missing-income inputs"}).raise_for_status()
                client.post(f"/api/v1/alerts/{alert_id}/close", headers={**headers, "Idempotency-Key": "a4-close"},
                            json={"note": "Confirmed intentionally injected synthetic missing values"}).raise_for_status()
                closed = client.get(f"/api/v1/alerts/{alert_id}", headers=headers).json()
                if closed["status"] != "closed" or len(closed["actions"]) != 2:
                    raise RuntimeError("alert workflow did not preserve its action history")
                with closing(sqlite3.connect(database)) as conn:
                    counts = {name: conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
                              for name in ("predictions", "outbox_events", "processed_events", "monitor_windows", "metrics", "alerts", "metric_revisions")}
                    queue = dict(conn.execute("SELECT status,COUNT(*) FROM outbox_events GROUP BY status"))
                if counts["processed_events"] != 40 or queue != {"done": 40}:
                    raise RuntimeError("consumer markers and outbox did not reconcile")
                return {"passed": True, "date_shanghai": now_utc().astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d"),
                        "transport": "real loopback scoring and query HTTP; two independent worker processes",
                        "synthetic_only": True, "credentials_exported": False,
                        "source_timestamps_rewritten": False, "finalization_clock_advanced_seconds": 120,
                        "latency_benchmark": False, "database_removed_after_verification": True,
                        "queue_before_worker": before["queue"], "worker_process_runs": runs,
                        "test_clock_finalization": finalize, "database_counts": counts, "queue_after": queue,
                        "metric_samples": metric_rows, "closed_alert_sample": closed}
        finally:
            if server.poll() is None:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(server.pid), "/T", "/F"], stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW, check=False)
                else:
                    server.terminate()
                try:
                    server.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=5)
            if server.stdout:
                server.stdout.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=ROOT / "evidence/A4-worker-smoke.json")
    args = parser.parse_args()
    result = verify()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    with args.report.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")
    print(json.dumps({"passed": result["passed"], "worker_process_runs": result["worker_process_runs"],
                      "database_counts": result["database_counts"], "latency_benchmark": False}))


if __name__ == "__main__":
    main()
