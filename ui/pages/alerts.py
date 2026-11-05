import streamlit as st

from ui.common import METRICS, can_manage, client, heading, identity, mutate, pagination, version_picker


def render():
    heading("告警处置", "将触发证据与处置流程并列核对；迟到数据修正不会自动重开已关闭告警。")
    version, _ = version_picker()
    offset = pagination("alerts")
    rows = client().page("/api/v1/alerts", version["model_version_id"], offset)
    status = st.selectbox("处置状态（本页）", ["全部", "open", "acknowledged", "closed"])
    rows = [r for r in rows if status == "全部" or r["status"] == status]
    if not rows:
        st.info("当前页与筛选条件下暂无告警。可检查其他页及 Worker 状态。")
        return
    st.dataframe([{"告警 ID": r["alert_id"], "指标": METRICS[r["metric_name"]], "特征": r["feature"] or "—",
                   "数值": r["value"], "样本数": r["sample_count"], "处置状态": r["status"],
                   "当前仍触发": r["condition_active"], "规则版本": r["rule_snapshot"]["rule_version"]} for r in rows],
                  hide_index=True, width="stretch")
    chosen = st.selectbox("查看告警证据", [r["alert_id"] for r in rows])
    detail = client().request("GET", f"/api/v1/alerts/{chosen}")
    rule = detail["rule_snapshot"]
    st.subheader("触发原因")
    st.write(f"{METRICS[detail['metric_name']]} / {detail['feature'] or '整体'}：数值 {detail['value']}，"
             f"阈值 > {rule['threshold']}；样本数 {detail['sample_count']}，要求 ≥ {rule['min_samples']}。")
    st.caption(f"窗口 ID：{detail['window_id']} · 规则版本：{rule['rule_version']} · 窗口修订：{detail['window_revision']}")
    st.info(f"处置状态：{detail['status']}；当前触发条件：{'成立' if detail['condition_active'] else '已随修订消退'}。")
    with st.expander("冻结规则与完整证据"):
        st.json({k: v for k, v in detail.items() if k not in {"revisions", "actions"}})
    if can_manage() and detail["status"] != "closed":
        closing = detail["status"] == "acknowledged"
        allowed = not closing or "admin" in identity()["roles"] or detail["assigned_to"] == identity()["principal_id"]
        if allowed:
            with st.form(f"action-{chosen}"):
                note = st.text_area("处置说明（关闭时必填）", max_chars=1000)
                if st.form_submit_button("关闭告警" if closing else "认领告警"):
                    if closing and not note.strip():
                        st.warning("关闭前请填写处置说明。")
                    else:
                        mutate(f"/api/v1/alerts/{chosen}/{'close' if closing else 'acknowledge'}", {"note": note})
    with st.expander("修订历史与操作留痕"):
        st.json({"revisions": detail["revisions"], "actions": detail["actions"]})
