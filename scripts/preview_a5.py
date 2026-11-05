"""Start an isolated synthetic API/UI demo; Ctrl+C closes its child processes.

Uses a test clock advance for settlement, never edits source timestamps.
Credentials and database stay in ignored data/runtime/a5-preview/.
"""

from datetime import timedelta
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time

import httpx

from app.modules.monitoring.store import MonitoringStore
from app.modules.monitoring.time_utils import now_utc
from app.storage.sqlite_store import SqlitePredictionStore
from app.worker import MonitoringWorker

ROOT = Path(__file__).resolve().parents[1]


def main():
    folder = ROOT / "data/runtime/a5-preview"
    folder.mkdir(parents=True, exist_ok=True)
    if (folder / "platform.db").exists():
        raise SystemExit("Preview database exists. Use a fresh -- no overwrite is performed. Rename data/runtime/a5-preview first.")
    admin, scorer = secrets.token_urlsafe(48), secrets.token_urlsafe(48)
    env = {**os.environ, "DATABASE_URL": "sqlite:///" + (folder / "platform.db").as_posix(),
           "APP_ADMIN_TOKEN": admin, "APP_SCORER_TOKEN": scorer, "ARTIFACT_ROOT": str(ROOT / "artifacts"),
           "API_BASE_URL": "http://127.0.0.1:18005"}
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    processes, logs = [], []

    def start(args, name):
        log = (folder / f"{name}.log").open("w", encoding="utf-8")
        logs.append(log)
        child = subprocess.Popen([sys.executable, *args], cwd=ROOT, env=env, stdout=log,
                                 stderr=subprocess.STDOUT, creationflags=flags)
        processes.append(child)
        return child

    try:
        start(["-m", "uvicorn", "app.main:create_app", "--factory", "--host", "127.0.0.1", "--port", "18005"], "api")
        with httpx.Client(base_url=env["API_BASE_URL"], timeout=10, trust_env=False) as api:
            for _ in range(200):
                try:
                    if api.get("/health").status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                time.sleep(0.1)
            else:
                raise RuntimeError("Preview API failed to start; inspect the ignored API log")
            headers = {"Authorization": "Bearer " + admin, "Idempotency-Key": "preview-activate"}
            versions = api.get("/api/v1/model-versions", headers=headers).json()["items"]
            version_id = next(v["model_version_id"] for v in versions if v["version"] == "credit-logreg-v1")
            api.post(f"/api/v1/model-versions/{version_id}/activate", headers=headers).raise_for_status()
            raw = json.loads((ROOT / "examples/A2-score-input.json").read_text(encoding="utf-8"))
            for index in range(40):
                sample = {**raw, "customer_ref": f"SYN-A5-{index:04d}"}
                if index >= 30:
                    sample["monthly_income"] = None
                api.post("/api/v1/predictions", headers={"Authorization": "Bearer " + scorer,
                    "Idempotency-Key": f"preview-score-{index}"}, json={"model_version_id": version_id, "input": sample}).raise_for_status()
            base = SqlitePredictionStore(env["DATABASE_URL"])
            try:
                monitor = MonitoringStore(base)
                monitor.initialize()
                worker = MonitoringWorker(monitor, ROOT / "artifacts")
                worker.run_once(max_events=100)
                advanced = now_utc() + timedelta(seconds=120)
                MonitoringWorker(monitor, ROOT / "artifacts", clock=lambda: advanced).run_once()
            finally:
                base.close()
            metric_rows = api.get("/api/v1/metrics", headers=headers,
                                  params={"model_version_id": version_id, "limit": 100}).json()["items"]
            alert_rows = api.get("/api/v1/alerts", headers=headers,
                                 params={"model_version_id": version_id, "limit": 100}).json()["items"]
            assert metric_rows and alert_rows
            (ROOT / "evidence/A5-preview-seed.json").write_text(json.dumps({"passed": True, "synthetic_predictions": 40,
                "metrics": len(metric_rows), "alerts": len(alert_rows), "test_clock_advance_seconds": 120,
                "source_timestamps_modified": False, "performance_benchmark": False}, indent=2) + "\n", encoding="utf-8")
        start(["-m", "app.worker"], "worker")
        start(["-m", "streamlit", "run", "streamlit_app.py", "--server.address", "127.0.0.1", "--server.port", "18505",
               "--server.headless", "true", "--browser.gatherUsageStats", "false"], "ui")
        (folder / "credentials.json").write_text(json.dumps({"admin_token": admin, "scorer_token": scorer,
            "url": "http://127.0.0.1:18505", "child_pids": [p.pid for p in processes]}), encoding="utf-8")
        print("A5 preview ready: http://127.0.0.1:18505 ; credentials: data/runtime/a5-preview/credentials.json", flush=True)
        while all(p.poll() is None for p in processes):
            time.sleep(1)
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True)
                else:
                    process.terminate()
                process.wait(timeout=10)
        for log in logs:
            log.close()


if __name__ == "__main__":
    main()
