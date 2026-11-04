"""Exercise a real loopback HTTP server and inspect its temporary SQLite outbox.

Only synthetic examples are sent. Generated credentials and runtime DBs are never exported.
"""

import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time

import httpx

ROOT = Path(__file__).resolve().parents[1]


def verify_http() -> dict:
    runtime = ROOT / "data" / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=runtime, prefix="a3-http-") as temp:
        folder = Path(temp).resolve()
        if not folder.is_relative_to(runtime.resolve()):
            raise RuntimeError("temporary directory escaped the runtime workspace")
        # The only file we temporarily move is inside this disposable copy.
        copied = folder / "artifacts"
        shutil.copytree(ROOT / "artifacts", copied)
        database = folder / "platform.db"
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        admin_token, scorer_token = secrets.token_urlsafe(48), secrets.token_urlsafe(48)
        env = {**os.environ, "DATABASE_URL": "sqlite:///" + database.as_posix(),
               "ARTIFACT_ROOT": str(copied), "APP_ADMIN_TOKEN": admin_token,
               "APP_SCORER_TOKEN": scorer_token, "INFERENCE_TIMEOUT_MS": "2000"}
        command = [sys.executable, "-m", "uvicorn", "app.main:create_app", "--factory",
                   "--host", "127.0.0.1", "--port", str(port), "--log-level", "error"]
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, creationflags=creationflags)
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5, trust_env=False) as client:
                deadline = time.monotonic() + 25
                while True:
                    if process.poll() is not None:
                        raise RuntimeError("local API exited during startup")
                    try:
                        if client.get("/health").status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    if time.monotonic() >= deadline:
                        raise RuntimeError("local API startup timed out")
                    time.sleep(0.1)
                admin = {"Authorization": "Bearer " + admin_token, "Idempotency-Key": "smoke-activate-v1"}
                scorer = {"Authorization": "Bearer " + scorer_token, "Idempotency-Key": "smoke-normal"}
                versions = client.get("/api/v1/model-versions", headers=admin)
                versions.raise_for_status()
                ids = {item["version"]: item["model_version_id"] for item in versions.json()["items"]}
                activation = client.post(f"/api/v1/model-versions/{ids['credit-logreg-v1']}/activate", headers=admin)
                activation.raise_for_status()
                payload = json.loads((ROOT / "examples/A3-score-request.json").read_text(encoding="utf-8"))
                payload["model_version_id"] = ids["credit-logreg-v1"]
                normal = client.post("/api/v1/predictions", headers=scorer, json=payload)
                repeated = client.post("/api/v1/predictions", headers=scorer, json=payload)
                if normal.status_code != 201 or normal.json() != repeated.json() or repeated.headers["Idempotency-Replayed"] != "true":
                    raise RuntimeError("normal or idempotent HTTP request failed")
                conflict_payload = {**payload, "input": {**payload["input"], "debt_income_ratio": 0.8}}
                conflict = client.post("/api/v1/predictions", headers=scorer, json=conflict_payload)
                if conflict.status_code != 409:
                    raise RuntimeError("conflicting key was not rejected")
                model_path = copied / "credit-logreg-v1" / "pipeline.joblib"
                disabled_path = model_path.with_suffix(".disabled")
                if not model_path.resolve().is_relative_to(folder):
                    raise RuntimeError("model fault injection escaped the disposable workspace")
                model_path.rename(disabled_path)
                try:
                    failed = client.post("/api/v1/predictions", headers={**scorer, "Idempotency-Key": "smoke-failure"}, json=payload)
                finally:
                    disabled_path.rename(model_path)
                if failed.status_code != 503 or failed.json().get("error_code") != "MODEL_PACKAGE_UNAVAILABLE":
                    raise RuntimeError("package failure was not recorded correctly")
                failed_replay = client.post("/api/v1/predictions", headers={**scorer, "Idempotency-Key": "smoke-failure"}, json=payload)
                if failed_replay.json() != failed.json():
                    raise RuntimeError("failed record was not idempotent")
                admin["Idempotency-Key"] = "smoke-activate-v2"
                client.post(f"/api/v1/model-versions/{ids['credit-logreg-v2']}/activate", headers=admin).raise_for_status()
                changed = client.post("/api/v1/predictions", headers={**scorer, "Idempotency-Key": "smoke-v2"},
                                      json={**payload, "model_version_id": ids["credit-logreg-v2"]})
                changed.raise_for_status()
                unauthorized = client.get("/api/v1/model-versions")
                if unauthorized.status_code != 401:
                    raise RuntimeError("missing authentication was not rejected")
                with closing(sqlite3.connect(database)) as conn:
                    counts = {name: conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
                              for name in ("input_snapshots", "predictions", "audit_logs", "outbox_events")}
                    event_samples = [json.loads(row[0]) for row in conn.execute("SELECT payload FROM outbox_events ORDER BY created_at")]
                    statuses = [row[0] for row in conn.execute("SELECT status FROM outbox_events")]
                if counts != {"input_snapshots": 3, "predictions": 3, "audit_logs": 5, "outbox_events": 3}:
                    raise RuntimeError("database evidence counts do not reconcile")
                if statuses != ["pending"] * 3:
                    raise RuntimeError("events must remain pending before A4 exists")
                return {"passed": True, "transport": "real HTTP on 127.0.0.1", "synthetic_only": True,
                        "credentials_exported": False, "database": "temporary SQLite; removed after verification",
                        "checks": {"normal_http_status": normal.status_code, "replay_same_body": True,
                                   "conflict_http_status": conflict.status_code, "failure_http_status": failed.status_code,
                                   "unauthenticated_http_status": unauthorized.status_code,
                                   "failed_replay_same_body_after_package_restored": True},
                        "database_counts": counts, "outbox_statuses": statuses, "normal_response": normal.json(),
                        "failed_response": failed.json(), "v2_response": changed.json(), "outbox_events": event_samples}
        finally:
            if process.poll() is None:
                if os.name == "nt":
                    # Windows venv redirectors can have a child Python process.
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   creationflags=subprocess.CREATE_NO_WINDOW, check=False)
                else:
                    process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            if process.stdout:
                process.stdout.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=ROOT / "evidence" / "A3-http-smoke.json")
    args = parser.parse_args()
    result = verify_http()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    with args.report.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")
    print(json.dumps({"passed": result["passed"], "transport": result["transport"],
                      "database_counts": result["database_counts"]}))


if __name__ == "__main__":
    main()
