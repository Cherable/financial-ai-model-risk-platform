"""Single-process monitoring worker. Run `python -m app.worker --once` or continuously."""

import argparse
import json
import time
from uuid import UUID, uuid4

from sqlalchemy.exc import SQLAlchemyError

from app.config import Settings
from app.contracts import DomainError, StorageUnavailable
from app.demo_auth import DEMO_MODEL_ID
from app.modules.models.bundle import load_bundle
from app.modules.monitoring.statistics import calculate
from app.modules.monitoring.store import InvalidEvent, LeaseLost, MonitoringStore
from app.modules.monitoring.time_utils import now_utc
from app.storage.sqlite_store import SqlitePredictionStore


class MonitoringWorker:
    def __init__(self, store: MonitoringStore, artifact_root, *, clock=now_utc, before_publish=None):
        self.store, self.artifact_root, self.clock = store, artifact_root, clock
        self.worker_id = str(uuid4())
        self.before_publish = before_publish or (lambda window: None)

    def run_once(self, max_events=100, max_windows=20):
        if max_events < 1 or max_windows < 1:
            raise ValueError("batch sizes must be positive")
        result = {"consumed": 0, "duplicates": 0, "event_errors": 0, "lease_lost": 0,
                  "windows_computed": 0, "window_errors": 0, "stale_calculations": 0}
        for _ in range(max_events):
            claimed = self.store.claim(self.clock())
            if claimed is None:
                break
            try:
                added = self.store.consume(claimed, self.clock())
                result["consumed" if added else "duplicates"] += 1
            except LeaseLost:
                result["lease_lost"] += 1
            except Exception as exc:
                self.store.fail_event(claimed, self.clock(), "INVALID_EVENT" if isinstance(exc, (InvalidEvent, ValueError, KeyError, TypeError)) else "CONSUME_FAILED")
                result["event_errors"] += 1
        for pending in self.store.ready_windows(self.clock(), max_windows):
            window, version, records = self.store.window_data(pending["window_id"])
            try:
                bundle = load_bundle(version["version"], version["manifest_sha256"], self.artifact_root)
            except Exception:
                self.store.fail_window(window, self.clock(), "REFERENCE_UNAVAILABLE")
                result["window_errors"] += 1
                continue
            try:
                if len(records) != window["event_count"]:
                    raise ValueError("window count mismatch")
                rows = calculate(records, bundle.reference_distribution)
                self.before_publish(window)
                published = self.store.publish(window, rows, bundle.manifest_sha256, self.clock())
                result["windows_computed" if published else "stale_calculations"] += 1
            except Exception:
                self.store.fail_window(window, self.clock(), "CALCULATION_FAILED")
                result["window_errors"] += 1
        self.store.heartbeat(self.worker_id, self.clock(), result)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--max-events", type=int, default=100)
    parser.add_argument("--poll-seconds", type=float, default=5)
    retries = parser.add_mutually_exclusive_group()
    retries.add_argument("--retry-event", type=UUID)
    retries.add_argument("--retry-window", type=UUID)
    parser.add_argument("--reason", default="")
    args = parser.parse_args()
    if not 0 < args.poll_seconds <= 60 or args.max_events < 1:
        parser.error("poll seconds must be in (0,60] and max-events must be positive")
    if (args.retry_event or args.retry_window) and (not args.reason.strip() or len(args.reason) > 1000):
        parser.error("manual retry requires a nonempty --reason of at most 1000 characters")
    settings = Settings.from_env()
    base = SqlitePredictionStore(settings.database_url)
    try:
        base.initialize([])
        if not base.versions(frozenset({DEMO_MODEL_ID})):
            parser.error("no registered versions; initialize the A3 API against this database first")
        store = MonitoringStore(base)
        store.initialize()
        if args.retry_event or args.retry_window:
            kind = "event" if args.retry_event else "window"
            print(json.dumps(store.retry_failed(kind, str(args.retry_event or args.retry_window), args.reason, now_utc())))
            return
        worker = MonitoringWorker(store, settings.artifact_root)
        while True:
            try:
                print(json.dumps(worker.run_once(args.max_events)), flush=True)
            except (StorageUnavailable, SQLAlchemyError):
                print(json.dumps({"error": "STORAGE_UNAVAILABLE"}), flush=True)
                if args.once:
                    raise SystemExit(1)
            if args.once:
                break
            time.sleep(args.poll_seconds)
    except DomainError as exc:
        print(json.dumps({"error": exc.code, "message": exc.message}), flush=True)
        raise SystemExit(1)
    except KeyboardInterrupt:
        pass
    finally:
        base.close()


if __name__ == "__main__":
    main()
