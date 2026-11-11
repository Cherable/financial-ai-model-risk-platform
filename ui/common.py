"""Shared presentation and session state; deliberately no cross-session caches."""

from datetime import datetime, timezone
import os

import streamlit as st

from ui.api_client import ApiClient, ApiError, command_key


def client():
    return ApiClient(os.environ.get("API_BASE_URL", "http://127.0.0.1:8000"), st.session_state["token"])


def identity():
    return st.session_state["identity"]


def can_manage():
    return bool(set(identity()["roles"]) & {"admin", "risk_manager"})


def error(exc):
    if exc.status == 401:
        st.session_state.clear()
        st.error("会话已失效，请重新连接。")
        st.stop()
    st.error(str(exc))
    hints = {403: "当前角色或模型授权不足。", 409: "数据状态已变化，请刷新核对后再操作。",
             422: "请检查输入格式、阈值、样本下限及生效时间。", 503: "服务暂不可用；重试同一操作会沿用幂等键。"}
    st.info(hints.get(exc.status, "请检查 API 服务后重试；同一操作会沿用幂等键。"))


def mutate(path, body):
    key = command_key(st.session_state, path, body)
    try:
        result = client().request("POST", path, body=body, key=key)
    except ApiError as exc:
        error(exc)
        return None
    # Acknowledged success ends this command. A later intentional activation of
    # the same version must not replay a historical activation receipt forever.
    st.session_state["commands"].pop(path, None)
    st.success("操作已保存。点击刷新可核对最新状态。")
    return result


def heading(title, description):
    title_column, refresh_column = st.columns([3, 1], vertical_alignment="center")
    with title_column:
        st.title(title)
    with refresh_column:
        if st.button("刷新数据", key="refresh", width="stretch"):
            st.rerun()
    st.caption(description)
    st.caption(f"本次读取：{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')} · 教学合成数据")


def version_picker():
    versions = client().request("GET", "/api/v1/model-versions")["items"]
    if not versions:
        st.info("当前账号没有可访问的模型版本。")
        st.stop()
    by_id = {v["model_version_id"]: v for v in versions}
    chosen = st.selectbox("模型版本", list(by_id), key="selected_version",
                          format_func=lambda key: f"{by_id[key]['version']} · {'已激活' if by_id[key]['active'] else '未激活'}")
    st.caption(f"版本 ID：{chosen}")
    return by_id[chosen], versions


def pagination(prefix, size=30):
    page = st.number_input("页码", min_value=1, step=1, value=1, key=f"{prefix}_page")
    st.caption(f"每页 {size} 条；空页表示当前没有更多记录。筛选仅作用于本页。")
    return (page - 1) * size


METRICS = {"missing_rate": "特征缺失率", "feature_psi": "特征 PSI", "prediction_psi": "预测 PSI",
           "failure_rate": "推理失败率", "inference_p95": "推理延迟 P95"}
FEATURES = ["age", "monthly_income", "debt_income_ratio", "past_due_count", "loan_amount", "employment_years"]
FEATURE_NAMES = {"age": "年龄", "monthly_income": "月收入", "debt_income_ratio": "负债收入比",
                 "past_due_count": "历史逾期次数", "loan_amount": "贷款金额", "employment_years": "工作年限"}
