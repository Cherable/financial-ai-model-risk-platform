"""Run the real acceptance suite and save its machine-readable result."""

import argparse
from datetime import datetime, timedelta, timezone
from importlib.metadata import version
import json
from pathlib import Path
import sys
import unittest

from app.modules.models.bundle import environment

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=ROOT / "evidence" / "A5-validation-summary.json")
    args = parser.parse_args()
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
    result = unittest.TextTestRunner(stream=sys.stderr, verbosity=2).run(suite)
    smoke = ROOT / "evidence" / "A3-http-smoke.json"
    worker_smoke = ROOT / "evidence" / "A4-worker-smoke.json"
    report = {
        "date_shanghai": datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d"),
        "scope": "A1-A4 regression and A5 HTTP client, identity and Streamlit widget integration",
        "environment": environment(),
        "api_packages": {p: version(p) for p in ("fastapi", "uvicorn", "sqlalchemy", "httpx", "pydantic", "starlette", "anyio")},
        "ui_packages": {p: version(p) for p in ("streamlit", "pandas", "altair")},
        "unit_and_integration_tests": {"run": result.testsRun,
            "passed": result.testsRun - len(result.failures) - len(result.errors) - len(result.skipped),
            "failures": [case.id() for case, _ in result.failures],
            "errors": [case.id() for case, _ in result.errors], "skipped": len(result.skipped),
            "successful": result.wasSuccessful()},
        "loopback_http_evidence_passed": json.loads(smoke.read_text(encoding="utf-8"))["passed"] if smoke.exists() else False,
        "member_b_real_integration": "pending; using replaceable SQLite and demo-token adapters",
        "monitor_worker": "implemented as an independent process; metrics use durable consumed-event checkpoints",
        "worker_process_evidence_passed": json.loads(worker_smoke.read_text(encoding="utf-8"))["passed"] if worker_smoke.exists() else False,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    with args.report.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    raise SystemExit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    main()
