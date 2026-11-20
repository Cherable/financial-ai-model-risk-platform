import streamlit as st

from ui.api_client import ApiError
from ui.common import can_audit, client, error, heading


def evidence_view(detail):
    st.subheader("评分证据")
    columns = st.columns(4)
    columns[0].metric("风险概率", detail["risk_probability"] if detail["risk_probability"] is not None else "—")
    columns[1].metric("决策阈值", detail["decision_threshold"] if detail["decision_threshold"] is not None else "—")
    columns[2].metric("推理耗时", f"{detail['inference_ms']:.2f} ms" if detail["inference_ms"] is not None else "—")
    columns[3].metric("状态", detail["status"])
    st.caption(f"模型版本 {detail['model_version']} · 版本 ID {detail['model_version_id']}")
    st.caption(f"受理 {detail['request_received_at']} · 完成 {detail['completed_at']} · 窗口 {detail['window_id']}")
    if detail.get("raw_input_withheld"):
        st.info("原始输入仅对审计人员与管理员展示；当前显示规范化输入与缺失字段。")
    else:
        st.json({"原始输入": detail["original_input"]}, expanded=False)
    st.json({"规范化输入": detail["normalized_input"], "缺失字段": detail["missing_fields"]}, expanded=False)
    with st.expander("解释快照"):
        st.json(detail["explanation"] or {})
    if detail["alerts"]:
        st.write("本窗口告警")
        st.dataframe(detail["alerts"], hide_index=True, width="stretch")
    if detail["alert_actions"]:
        with st.expander("告警处置记录"):
            st.dataframe(detail["alert_actions"], hide_index=True, width="stretch")


def render():
    heading("审计查询", "按时间范围检索评分记录并核对证据链；查询与导出动作本身会写入审计日志。")
    if not can_audit():
        st.info("审计查询需要 audit 或 admin 角色。请使用审计账号登录。")
        return

    versions = client().request("GET", "/api/v1/model-versions")["items"]
    labels = {"全部版本": None}
    for version in versions:
        labels[f"{version['version']} · {version['model_version_id'][:8]}"] = version["model_version_id"]

    first, second, third, fourth = st.columns([2, 2, 2, 1])
    with first:
        start = st.date_input("起始日期（UTC，含）")
    with second:
        end = st.date_input("结束日期（UTC，含）")
    with third:
        chosen_label = st.selectbox("模型版本", list(labels))
    with fourth:
        status = st.selectbox("结果", ["全部", "success", "failed"])

    if end < start:
        st.warning("结束日期不能早于起始日期。")
        return

    page = st.number_input("页码", min_value=1, step=1, value=1, key="audit_page")
    params = {"from": f"{start.isoformat()}T00:00:00Z",
              "to": f"{end.isoformat()}T23:59:59Z",
              "offset": (page - 1) * 30, "limit": 30}
    if labels[chosen_label]:
        params["model_version_id"] = labels[chosen_label]
    if status != "全部":
        params["status"] = status

    try:
        records = client().request("GET", "/api/v1/audit/records", params=params)
    except ApiError as exc:
        error(exc)
        return

    items = records["items"]
    st.caption(f"本页 {len(items)} 条；时间范围最长 31 天。")
    if not items:
        st.info("当前条件下没有记录。请调整日期范围或页码。")
        return
    st.dataframe([{"评分编号": row["prediction_id"], "结果": row["status"],
                   "风险概率": row["risk_probability"], "推理耗时(ms)": row["inference_ms"],
                   "受理时间": row["request_received_at"]} for row in items],
                 hide_index=True, width="stretch")

    chosen = st.selectbox("查看证据", [row["prediction_id"] for row in items])
    try:
        detail = client().request("GET", f"/api/v1/predictions/{chosen}")
    except ApiError as exc:
        error(exc)
        return
    evidence_view(detail)

    st.divider()
    left, right = st.columns(2)
    if left.button("准备 JSON 导出", width="stretch"):
        content, headers = client().download(f"/api/v1/audit/records/{chosen}/export", params={"format": "json"})
        st.session_state["audit_export"] = (chosen, "json", content, headers.get("x-file-sha256", ""))
    if right.button("准备 CSV 导出", width="stretch"):
        content, headers = client().download(f"/api/v1/audit/records/{chosen}/export", params={"format": "csv"})
        st.session_state["audit_export"] = (chosen, "csv", content, headers.get("x-file-sha256", ""))

    export = st.session_state.get("audit_export")
    if export and export[0] == chosen:
        _, fmt, content, digest = export
        st.download_button(
            f"下载 {fmt.upper()}（SHA-256 {digest[:12]}…）", data=content,
            file_name=f"{chosen}.{fmt}",
            mime="application/json" if fmt == "json" else "text/csv",
            width="stretch")
        st.caption("导出动作已写入审计日志；文件摘要可与服务端记录核对。")
