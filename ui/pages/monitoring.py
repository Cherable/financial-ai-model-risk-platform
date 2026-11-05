from datetime import datetime, timedelta, timezone

import pandas as pd
import streamlit as st

from ui.common import (FEATURES, METRICS, can_manage, client, heading, identity,
                       mutate, pagination, version_picker)


def render():
    heading("监测指标", "按固定窗口观察数据质量、分布漂移和推理服务；PSI 至少需要 200 个成功样本。")
    version, _ = version_picker()
    if "admin" in identity()["roles"]:
        status = client().request("GET", "/api/v1/monitor/status")
        a, b, c = st.columns(3)
        a.metric("Worker 状态", status["worker"]["state"])
        b.metric("待消费事件", status["queue"].get("pending", 0) + status["queue"].get("processing", 0))
        c.metric("隔离 / 失败窗口", f"{status['queue'].get('dead_letter', 0)} / {status['failed_windows']}")
        if status["worker"]["state"] != "fresh":
            st.warning("Worker 未运行或心跳已过期；请检查独立监测进程。")
        with st.expander("Worker 与积压诊断"):
            st.json(status)
    offset = pagination("metrics")
    rows = client().page("/api/v1/metrics", version["model_version_id"], offset)
    if not rows:
        st.info("此页暂无指标。评分事件需由 Worker 消费，并在窗口结束 10 秒后结算。")
    else:
        selected = st.selectbox("指标类型", ["全部", *METRICS], format_func=lambda x: METRICS.get(x, x))
        shown = [r for r in rows if selected == "全部" or r["metric_name"] == selected]
        if any(r["stale"] for r in shown):
            st.warning("存在待更新指标：有事件尚未消费或窗口修订尚未计算。请勿视为最终结论。")
        table = [{"窗口开始 UTC": r["window_start"], "窗口结束 UTC": r["window_end"],
                  "指标": METRICS[r["metric_name"]], "特征": r["feature"] or "—", "数值": r["value"],
                  "单位": r["unit"], "样本数": r["sample_count"], "统计状态": r["status"],
                  "待更新": r["stale"], "计算修订": r["window_revision"], "最新修订": r["latest_window_revision"]} for r in shown]
        st.dataframe(table, hide_index=True, width="stretch")
        st.caption("空值代表样本不足等不可计算状态；比例取值 0—1。窗口使用 [开始, 结束) 区间。")
        choices = sorted({(r["metric_name"], r["feature"] or "") for r in shown})
        if choices:
            series = st.selectbox("趋势序列（本页）", choices, format_func=lambda p: f"{METRICS[p[0]]} / {p[1] or '整体'}")
            points = [{"window": r["window_start"], "value": r["value"]} for r in shown
                      if (r["metric_name"], r["feature"] or "") == series and not r["stale"] and r["value"] is not None]
            if points:
                st.line_chart(pd.DataFrame(points).sort_values("window").set_index("window"))
            else:
                st.info("此序列尚无可绘制的有效指标。")
        with st.expander("参考清单、统计详情与修订证据"):
            st.json(shown)
    st.subheader("规则版本")
    rule_offset = pagination("rules")
    rules = client().page("/api/v1/monitor-rules", version["model_version_id"], rule_offset)
    st.dataframe(rules, hide_index=True, width="stretch")
    st.caption("本表包含历史与未来规则；每个窗口按开始时间冻结规则，新增规则不修改历史窗口。")
    if can_manage():
        with st.expander("新增规则版本"):
            # Outside the form so feature options and PSI limits rerender when the metric changes.
            metric = st.selectbox("规则指标", list(METRICS), format_func=METRICS.get)
            with st.form("rule"):
                feature = st.selectbox("规则特征", FEATURES) if metric in {"missing_rate", "feature_psi"} else None
                threshold = st.number_input("触发阈值（严格大于）", min_value=0.0, value=0.2, step=0.01,
                                            max_value=1.0 if metric in {"missing_rate", "failure_rate"} else None)
                minimum = st.number_input("最少样本数", min_value=200 if "psi" in metric else 1, max_value=100000,
                                          value=200, step=1)
                effective = st.text_input("生效时间（未来 UTC 整分钟，ISO 8601）",
                    value=(datetime.now(timezone.utc) + timedelta(minutes=2)).replace(second=0, microsecond=0).isoformat())
                if st.form_submit_button("保存新规则版本"):
                    mutate("/api/v1/monitor-rules", {"model_version_id": version["model_version_id"], "metric_name": metric,
                        "feature": feature, "threshold": threshold, "min_samples": minimum, "effective_at": effective})
