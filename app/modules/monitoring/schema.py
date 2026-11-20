"""Additive A4 tables sharing A3's metadata and foreign keys."""

from sqlalchemy import JSON, Boolean, Column, Float, ForeignKey, Index, Integer, String, Table, UniqueConstraint

from app.storage.sqlite_store import metadata

rules = Table("monitor_rules", metadata,
              Column("rule_id", String, primary_key=True),
              Column("model_version_id", ForeignKey("model_versions.model_version_id"), nullable=False),
              Column("metric_name", String, nullable=False), Column("feature", String, nullable=False),
              Column("rule_version", Integer, nullable=False), Column("threshold", Float, nullable=False),
              Column("min_samples", Integer, nullable=False), Column("effective_at", String, nullable=False),
              Column("created_at", String, nullable=False), Column("created_by", String, nullable=False),
              UniqueConstraint("model_version_id", "metric_name", "feature", "rule_version"))
windows = Table("monitor_windows", metadata,
                Column("window_id", String, primary_key=True),
                Column("model_version_id", ForeignKey("model_versions.model_version_id"), nullable=False),
                Column("window_start", String, nullable=False), Column("window_end", String, nullable=False),
                Column("revision", Integer, nullable=False), Column("computed_revision", Integer, nullable=False),
                Column("event_count", Integer, nullable=False), Column("success_count", Integer, nullable=False),
                Column("failure_count", Integer, nullable=False), Column("attempted_count", Integer, nullable=False),
                Column("rule_snapshot", JSON, nullable=False), Column("status", String, nullable=False),
                Column("calculation_attempts", Integer, nullable=False), Column("next_calculation_at", String),
                Column("last_error", String), Column("computed_at", String),
                UniqueConstraint("model_version_id", "window_start"))
processed = Table("processed_events", metadata,
                  Column("consumer", String, primary_key=True),
                  Column("event_id", ForeignKey("outbox_events.event_id"), primary_key=True),
                  Column("window_id", ForeignKey("monitor_windows.window_id"), nullable=False),
                  Column("processed_at", String, nullable=False))
metrics = Table("metrics", metadata,
                Column("metric_id", String, primary_key=True), Column("window_id", ForeignKey("monitor_windows.window_id"), nullable=False),
                Column("model_version_id", ForeignKey("model_versions.model_version_id"), nullable=False),
                Column("window_start", String, nullable=False), Column("window_end", String, nullable=False),
                Column("metric_name", String, nullable=False), Column("feature", String, nullable=False),
                Column("value", Float), Column("status", String, nullable=False), Column("unit", String, nullable=False),
                Column("sample_count", Integer, nullable=False), Column("details", JSON, nullable=False),
                Column("window_revision", Integer, nullable=False), Column("reference_manifest_sha256", String, nullable=False),
                Column("calculation_version", String, nullable=False), Column("computed_at", String, nullable=False),
                UniqueConstraint("window_id", "metric_name", "feature"))
metric_revisions = Table("metric_revisions", metadata,
                         Column("window_id", ForeignKey("monitor_windows.window_id"), primary_key=True),
                         Column("window_revision", Integer, primary_key=True), Column("metric_name", String, primary_key=True),
                         Column("feature", String, primary_key=True), Column("record", JSON, nullable=False))
alerts = Table("alerts", metadata,
               Column("alert_id", String, primary_key=True), Column("window_id", ForeignKey("monitor_windows.window_id"), nullable=False),
               Column("model_version_id", ForeignKey("model_versions.model_version_id"), nullable=False),
               Column("rule_id", ForeignKey("monitor_rules.rule_id"), nullable=False),
               Column("metric_name", String, nullable=False), Column("feature", String, nullable=False),
               Column("rule_snapshot", JSON, nullable=False), Column("value", Float), Column("sample_count", Integer, nullable=False),
               Column("condition_active", Boolean, nullable=False), Column("window_revision", Integer, nullable=False),
               Column("status", String, nullable=False), Column("assigned_to", String), Column("close_note", String),
               Column("created_at", String, nullable=False), Column("updated_at", String, nullable=False),
               UniqueConstraint("window_id", "rule_id"))
alert_revisions = Table("alert_revisions", metadata,
                        Column("alert_id", ForeignKey("alerts.alert_id"), primary_key=True),
                        Column("window_revision", Integer, primary_key=True), Column("record", JSON, nullable=False))
alert_actions = Table("alert_actions", metadata,
                      Column("action_id", String, primary_key=True), Column("alert_id", ForeignKey("alerts.alert_id"), nullable=False),
                      Column("principal_id", String, nullable=False), Column("from_status", String, nullable=False),
                      Column("to_status", String, nullable=False), Column("note", String, nullable=False), Column("created_at", String, nullable=False))
receipts = Table("monitor_receipts", metadata,
                 Column("principal_id", String, primary_key=True), Column("route", String, primary_key=True),
                 Column("idempotency_key", String, primary_key=True), Column("request_sha256", String, nullable=False),
                 Column("response_json", JSON, nullable=False))
worker_state = Table("monitor_worker_state", metadata,
                     Column("consumer", String, primary_key=True), Column("worker_id", String, nullable=False),
                     Column("last_seen_at", String, nullable=False), Column("last_result", JSON, nullable=False))
Index("ix_monitor_ready_windows", windows.c.window_end, windows.c.computed_revision, windows.c.revision)
Index("ix_metrics_version_window", metrics.c.model_version_id, metrics.c.window_start)
Index("ix_processed_window", processed.c.window_id, processed.c.consumer)
